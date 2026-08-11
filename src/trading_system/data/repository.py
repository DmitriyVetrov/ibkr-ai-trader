"""Data repository: raw evidence, normalised records, immutable snapshots.

The rest of the system asks this interface for data and never touches a path,
a filename or a JSON file. That is what keeps SQLite or PostgreSQL a later
implementation detail rather than a rewrite.

Four storage areas, with different rules, and mixing them up is the failure
this module exists to prevent:

``raw/``
    Provider responses exactly as received. Written once. If a provider returns
    something implausible, this is the proof.
``normalized/``
    Canonical records derived from raw. Regenerable, but written alongside the
    raw evidence so a normalisation bug is diagnosable after the fact.
``snapshots/``
    Immutable point-in-time captures. Never overwritten — a write to an
    existing snapshot id with different content raises.
``historical/``
    An append-only ledger per data type and key. Every snapshot creation,
    every unchanged re-observation, every failed collection. It is never
    rewritten, so a failure cannot erase earlier success, and it doubles as the
    index for point-in-time queries.

Cache lives outside all four (:mod:`trading_system.data.cache`) because it is
disposable and must never be mistaken for any of them.

Deduplication compares a new snapshot's payload hash against the newest stored
snapshot for the same data type, key and provider. Identical content is
recorded as a re-observation instead of a second snapshot; different content
always creates a new snapshot. Collection is therefore idempotent without ever
deduplicating away a real change.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from trading_system import __version__ as application_version
from trading_system.data.hashing import payload_hash, snapshot_identifier, stable_hash
from trading_system.data.models import (
    COLLECTION_FAILED,
    DATA_SCHEMA_VERSION,
    SNAPSHOT_CREATED,
    SNAPSHOT_REOBSERVED,
    CollectionState,
    DataQualityReport,
    DataRecord,
    DataSnapshot,
    RawRecord,
    SnapshotIndexEntry,
)
from trading_system.data.point_in_time import latest_as_of
from trading_system.domain.enums import DataType, MarketDataOrigin, SourceTier
from trading_system.infrastructure.clock import Clock, SystemClock

__all__ = [
    "DataRepository",
    "FilesystemDataRepository",
    "RepositoryError",
    "SnapshotIntegrityError",
    "SnapshotWriteResult",
    "build_snapshot",
    "records_of",
]

RecordT = TypeVar("RecordT", bound=DataRecord)

_SAFE_KEY = re.compile(r"[^A-Za-z0-9._-]")
_COMPACT_TIME = "%Y%m%dT%H%M%S%fZ"


class RepositoryError(RuntimeError):
    """The store could not satisfy a request."""


class SnapshotIntegrityError(RepositoryError):
    """A stored snapshot does not match its recorded hash, or was overwritten.

    Either is serious: a snapshot is evidence, and evidence that changed after
    the fact is worse than no evidence.
    """


@dataclass(frozen=True, slots=True)
class SnapshotWriteResult:
    """Outcome of offering a snapshot to the store."""

    snapshot_id: str
    created: bool
    reason: str

    @property
    def deduplicated(self) -> bool:
        return not self.created


# ---------------------------------------------------------------------------
# Construction helpers
# ---------------------------------------------------------------------------
def build_snapshot(
    *,
    data_type: DataType,
    key: str,
    records: Sequence[DataRecord],
    provider: str,
    source_tier: SourceTier,
    origin: MarketDataOrigin,
    as_of: datetime,
    retrieved_at: datetime,
    quality: DataQualityReport,
    source_timestamp: datetime | None = None,
    config_version: str | None = None,
    schema_version: str = DATA_SCHEMA_VERSION,
    application_version: str = application_version,
) -> DataSnapshot:
    """Assemble a snapshot from canonical records, hashing and stamping it.

    The identifier is derived, not generated: the same records collected from
    the same provider for the same instant always produce the same id, which is
    what makes deduplication and integrity checking possible.
    """
    serialised = [record.model_dump(mode="json") for record in records]
    content_hash = payload_hash(serialised)
    return DataSnapshot(
        snapshot_id=snapshot_identifier(
            data_type=data_type.value,
            key=key,
            provider=provider,
            schema_version=schema_version,
            as_of=as_of,
            content_hash=content_hash,
        ),
        data_type=data_type,
        key=key,
        as_of=as_of,
        retrieved_at=retrieved_at,
        source_timestamp=source_timestamp,
        provider=provider,
        source_tier=source_tier,
        data_origin=origin,
        schema_version=schema_version,
        application_version=application_version,
        config_version=config_version,
        data_quality=quality,
        payload_hash=content_hash,
        records=serialised,
        record_count=len(serialised),
    )


def records_of[RecordT: DataRecord](snapshot: DataSnapshot, model: type[RecordT]) -> list[RecordT]:
    """Re-validate a snapshot's stored records into canonical models.

    Validation on read is deliberate: a stored record that no longer satisfies
    the current model is a schema drift that must surface loudly rather than
    flow into a decision.
    """
    try:
        return [model.model_validate(row) for row in snapshot.records]
    except Exception as exc:
        raise SnapshotIntegrityError(
            f"snapshot {snapshot.snapshot_id} does not contain valid "
            f"{model.__name__} records (schema_version={snapshot.schema_version}): {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------
class DataRepository(ABC):
    """Storage interface for the data layer.

    No filesystem, SQL or serialisation detail may appear in this signature
    set — that is the whole point of it existing.
    """

    @abstractmethod
    def save_raw(self, record: RawRecord) -> str:
        """Persist a provider response verbatim. Returns its storage id."""

    @abstractmethod
    def save_normalized(
        self,
        *,
        data_type: DataType,
        key: str,
        provider: str,
        records: Sequence[DataRecord],
        as_of: datetime,
    ) -> str:
        """Persist canonical records derived from a raw response."""

    @abstractmethod
    def save_snapshot(self, snapshot: DataSnapshot) -> SnapshotWriteResult:
        """Offer an immutable snapshot to the store."""

    @abstractmethod
    def get_latest(
        self, data_type: DataType, key: str, *, provider: str | None = None
    ) -> DataSnapshot | None:
        """The most recent snapshot, regardless of when it became visible."""

    @abstractmethod
    def get_as_of(
        self, data_type: DataType, key: str, as_of: datetime, *, provider: str | None = None
    ) -> DataSnapshot | None:
        """The latest snapshot that was actually available at ``as_of``."""

    @abstractmethod
    def get_range(
        self,
        data_type: DataType,
        key: str,
        start: datetime,
        end: datetime,
        *,
        provider: str | None = None,
    ) -> list[DataSnapshot]:
        """Every snapshot describing an instant within ``[start, end]``."""

    @abstractmethod
    def exists(self, data_type: DataType, key: str, snapshot_id: str) -> bool: ...

    @abstractmethod
    def list_snapshots(
        self,
        data_type: DataType | None = None,
        key: str | None = None,
        *,
        provider: str | None = None,
        limit: int | None = None,
    ) -> list[SnapshotIndexEntry]:
        """Snapshot metadata, newest first, without loading payloads."""

    @abstractmethod
    def ledger(self, data_type: DataType, key: str) -> list[SnapshotIndexEntry]:
        """The full append-only history for one data type and key."""

    @abstractmethod
    def record_failure(
        self,
        *,
        data_type: DataType,
        key: str,
        provider: str,
        outcome: str,
        detail: str | None = None,
    ) -> None:
        """Record a failed collection without touching existing history."""

    @abstractmethod
    def save_collection_state(self, state: CollectionState) -> None: ...

    @abstractmethod
    def get_collection_state(
        self, *, provider: str, data_type: DataType, key: str
    ) -> CollectionState | None: ...

    @abstractmethod
    def list_collection_states(self) -> list[CollectionState]: ...

    @abstractmethod
    def keys(self, data_type: DataType) -> list[str]:
        """Every key with stored history for this data type."""


# ---------------------------------------------------------------------------
# Filesystem implementation
# ---------------------------------------------------------------------------
class FilesystemDataRepository(DataRepository):
    """JSON-on-disk store. The initial implementation; the interface is the API.

    Chosen over SQLite for the first pass because the artifacts are meant to be
    inspectable with ``cat`` and diffable in review, and because an immutable
    snapshot maps naturally onto a file that is written once.
    """

    def __init__(self, root: Path | str, *, clock: Clock | None = None) -> None:
        self._root = Path(root)
        self._clock = clock or SystemClock()

    @property
    def root(self) -> Path:
        return self._root

    # --- raw ---------------------------------------------------------------
    def save_raw(self, record: RawRecord) -> str:
        """Write a provider response verbatim.

        Refuses to overwrite: raw evidence is immutable, and a second response
        with identical content simply reuses the existing file.
        """
        path = (
            self._raw_dir(record.data_type, record.key)
            / f"{_compact(record.retrieved_at)}-{record.payload_hash[:12]}.json"
        )
        if path.exists():
            return str(path.relative_to(self._root))
        _write_json(path, record.model_dump(mode="json"))
        return str(path.relative_to(self._root))

    def save_normalized(
        self,
        *,
        data_type: DataType,
        key: str,
        provider: str,
        records: Sequence[DataRecord],
        as_of: datetime,
    ) -> str:
        serialised = [record.model_dump(mode="json") for record in records]
        content_hash = payload_hash(serialised)
        path = self._normalized_dir(data_type, key) / f"{_compact(as_of)}-{content_hash[:12]}.json"
        if not path.exists():
            _write_json(
                path,
                {
                    "data_type": data_type.value,
                    "key": key,
                    "provider": provider,
                    "as_of": as_of.astimezone(UTC).isoformat(),
                    "schema_version": DATA_SCHEMA_VERSION,
                    "payload_hash": content_hash,
                    "records": serialised,
                },
            )
        return str(path.relative_to(self._root))

    # --- snapshots ---------------------------------------------------------
    def save_snapshot(self, snapshot: DataSnapshot) -> SnapshotWriteResult:
        # Verify before anything else. Deduplication compares payload hashes,
        # so a snapshot whose declared hash does not describe its own records
        # could otherwise suppress a real snapshot or smuggle unhashed content
        # into the store.
        if not self.verify(snapshot):
            raise SnapshotIntegrityError(
                f"snapshot {snapshot.snapshot_id} declares payload_hash "
                f"{snapshot.payload_hash} but its records hash to "
                f"{payload_hash(snapshot.records)}"
            )

        entries = self._read_ledger(snapshot.data_type, snapshot.key)
        newest = self._newest_snapshot_entry(entries, provider=snapshot.provider)

        if newest is not None and newest.payload_hash == snapshot.payload_hash:
            self._append_ledger(
                SnapshotIndexEntry(
                    event=SNAPSHOT_REOBSERVED,
                    data_type=snapshot.data_type,
                    key=snapshot.key,
                    provider=snapshot.provider,
                    recorded_at=self._clock.now(),
                    snapshot_id=newest.snapshot_id,
                    payload_hash=snapshot.payload_hash,
                    retrieved_at=snapshot.retrieved_at,
                    detail="provider returned content identical to the newest snapshot",
                )
            )
            return SnapshotWriteResult(
                snapshot_id=str(newest.snapshot_id),
                created=False,
                reason="UNCHANGED",
            )

        path = self._snapshot_path(snapshot)
        if path.exists():
            stored = self._load_snapshot(path)
            if stored.payload_hash != snapshot.payload_hash:
                raise SnapshotIntegrityError(
                    f"refusing to overwrite snapshot {snapshot.snapshot_id}: a snapshot is "
                    f"immutable and the stored payload differs from the new one"
                )
            return SnapshotWriteResult(
                snapshot_id=snapshot.snapshot_id, created=False, reason="ALREADY_STORED"
            )

        _write_json(path, snapshot.model_dump(mode="json"))
        self._append_ledger(
            SnapshotIndexEntry(
                event=SNAPSHOT_CREATED,
                data_type=snapshot.data_type,
                key=snapshot.key,
                provider=snapshot.provider,
                recorded_at=self._clock.now(),
                snapshot_id=snapshot.snapshot_id,
                as_of=snapshot.as_of,
                retrieved_at=snapshot.retrieved_at,
                source_timestamp=snapshot.source_timestamp,
                payload_hash=snapshot.payload_hash,
                record_count=snapshot.record_count,
                research_usable=snapshot.data_quality.research_usable,
            )
        )
        return SnapshotWriteResult(snapshot_id=snapshot.snapshot_id, created=True, reason="CREATED")

    def get_latest(
        self, data_type: DataType, key: str, *, provider: str | None = None
    ) -> DataSnapshot | None:
        entries = self._snapshot_entries(data_type, key, provider=provider)
        if not entries:
            return None
        newest = max(entries, key=lambda e: (e.as_of or e.recorded_at, e.recorded_at))
        return self._load_by_entry(newest)

    def get_as_of(
        self, data_type: DataType, key: str, as_of: datetime, *, provider: str | None = None
    ) -> DataSnapshot | None:
        """The newest snapshot that was already knowable at ``as_of``.

        Visibility is decided from the ledger's timestamps, so a snapshot
        retrieved after ``as_of`` is never even loaded, let alone returned.
        """
        entries = self._snapshot_entries(data_type, key, provider=provider)
        chosen = latest_as_of(entries, as_of)
        return None if chosen is None else self._load_by_entry(chosen)

    def get_range(
        self,
        data_type: DataType,
        key: str,
        start: datetime,
        end: datetime,
        *,
        provider: str | None = None,
    ) -> list[DataSnapshot]:
        entries = [
            entry
            for entry in self._snapshot_entries(data_type, key, provider=provider)
            if entry.as_of is not None and start <= entry.as_of <= end
        ]
        entries.sort(key=lambda e: (e.as_of or e.recorded_at, e.recorded_at))
        return [self._load_by_entry(entry) for entry in entries]

    def exists(self, data_type: DataType, key: str, snapshot_id: str) -> bool:
        return any(
            entry.snapshot_id == snapshot_id
            for entry in self._snapshot_entries(data_type, key, provider=None)
        )

    def list_snapshots(
        self,
        data_type: DataType | None = None,
        key: str | None = None,
        *,
        provider: str | None = None,
        limit: int | None = None,
    ) -> list[SnapshotIndexEntry]:
        types = [data_type] if data_type is not None else list(DataType)
        entries: list[SnapshotIndexEntry] = []
        for candidate in types:
            keys = [key] if key is not None else self.keys(candidate)
            for candidate_key in keys:
                entries.extend(self._snapshot_entries(candidate, candidate_key, provider=provider))
        entries.sort(key=lambda e: (e.as_of or e.recorded_at, e.recorded_at), reverse=True)
        return entries[:limit] if limit is not None else entries

    def ledger(self, data_type: DataType, key: str) -> list[SnapshotIndexEntry]:
        return self._read_ledger(data_type, key)

    def record_failure(
        self,
        *,
        data_type: DataType,
        key: str,
        provider: str,
        outcome: str,
        detail: str | None = None,
    ) -> None:
        """Append a failure. Existing snapshots are not touched, ever."""
        self._append_ledger(
            SnapshotIndexEntry(
                event=COLLECTION_FAILED,
                data_type=data_type,
                key=key,
                provider=provider,
                recorded_at=self._clock.now(),
                outcome=outcome,
                detail=detail,
            )
        )

    # --- collection state --------------------------------------------------
    def save_collection_state(self, state: CollectionState) -> None:
        _write_json(
            self._state_path(state.provider, state.data_type, state.key),
            state.model_dump(mode="json"),
            overwrite=True,
        )

    def get_collection_state(
        self, *, provider: str, data_type: DataType, key: str
    ) -> CollectionState | None:
        path = self._state_path(provider, data_type, key)
        if not path.exists():
            return None
        return CollectionState.model_validate(_read_json(path))

    def list_collection_states(self) -> list[CollectionState]:
        root = self._root / "historical" / "_collection_state"
        if not root.is_dir():
            return []
        states = [
            CollectionState.model_validate(_read_json(p)) for p in sorted(root.rglob("*.json"))
        ]
        states.sort(key=lambda s: (s.provider, s.data_type.value, s.key))
        return states

    def keys(self, data_type: DataType) -> list[str]:
        directory = self._root / "historical" / data_type.value
        if not directory.is_dir():
            return []
        keys: list[str] = []
        for path in sorted(directory.glob("*.jsonl")):
            entries = self._read_ledger_file(path)
            if entries:
                keys.append(entries[0].key)
        return keys

    # --- integrity ---------------------------------------------------------
    def verify(self, snapshot: DataSnapshot) -> bool:
        """Whether a snapshot's stored records still hash to its recorded value."""
        return payload_hash(snapshot.records) == snapshot.payload_hash

    # --- internals ---------------------------------------------------------
    def _raw_dir(self, data_type: DataType, key: str) -> Path:
        return self._root / "raw" / data_type.value / _safe_key(key)

    def _normalized_dir(self, data_type: DataType, key: str) -> Path:
        return self._root / "normalized" / data_type.value / _safe_key(key)

    def _snapshot_dir(self, data_type: DataType, key: str) -> Path:
        return self._root / "snapshots" / data_type.value / _safe_key(key)

    def _ledger_path(self, data_type: DataType, key: str) -> Path:
        return self._root / "historical" / data_type.value / f"{_safe_key(key)}.jsonl"

    def _state_path(self, provider: str, data_type: DataType, key: str) -> Path:
        return (
            self._root
            / "historical"
            / "_collection_state"
            / _safe_key(provider)
            / data_type.value
            / f"{_safe_key(key)}.json"
        )

    def _snapshot_path(self, snapshot: DataSnapshot) -> Path:
        return self._snapshot_dir(snapshot.data_type, snapshot.key) / _snapshot_filename(
            snapshot.as_of, snapshot.snapshot_id
        )

    def _snapshot_entries(
        self, data_type: DataType, key: str, *, provider: str | None
    ) -> list[SnapshotIndexEntry]:
        return [
            entry
            for entry in self._read_ledger(data_type, key)
            if entry.event == SNAPSHOT_CREATED and (provider is None or entry.provider == provider)
        ]

    @staticmethod
    def _newest_snapshot_entry(
        entries: Iterable[SnapshotIndexEntry], *, provider: str
    ) -> SnapshotIndexEntry | None:
        candidates = [
            entry
            for entry in entries
            if entry.event == SNAPSHOT_CREATED and entry.provider == provider
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda e: (e.as_of or e.recorded_at, e.recorded_at))

    def _read_ledger(self, data_type: DataType, key: str) -> list[SnapshotIndexEntry]:
        return self._read_ledger_file(self._ledger_path(data_type, key))

    @staticmethod
    def _read_ledger_file(path: Path) -> list[SnapshotIndexEntry]:
        if not path.exists():
            return []
        entries: list[SnapshotIndexEntry] = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entries.append(SnapshotIndexEntry.model_validate(json.loads(line)))
            except Exception as exc:
                raise RepositoryError(f"corrupt ledger line {path}:{number}: {exc}") from exc
        return entries

    def _append_ledger(self, entry: SnapshotIndexEntry) -> None:
        path = self._ledger_path(entry.data_type, entry.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _load_by_entry(self, entry: SnapshotIndexEntry) -> DataSnapshot:
        if entry.as_of is None or entry.snapshot_id is None:  # pragma: no cover - defensive
            raise RepositoryError(f"ledger entry for {entry.key} is not a snapshot entry")
        path = self._snapshot_dir(entry.data_type, entry.key) / _snapshot_filename(
            entry.as_of, entry.snapshot_id
        )
        snapshot = self._load_snapshot(path)
        if not self.verify(snapshot):
            raise SnapshotIntegrityError(
                f"snapshot {snapshot.snapshot_id} no longer matches its recorded payload hash; "
                f"the stored file has been modified"
            )
        return snapshot

    @staticmethod
    def _load_snapshot(path: Path) -> DataSnapshot:
        if not path.exists():
            raise RepositoryError(f"snapshot file is missing: {path}")
        return DataSnapshot.model_validate(_read_json(path))


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------
def _safe_key(key: str) -> str:
    """Make a key safe as a path segment without losing its identity.

    A key that needed escaping gets a hash suffix, so ``BRK/B`` and ``BRK_B``
    cannot collide into the same directory.
    """
    cleaned = _SAFE_KEY.sub("_", key)
    if cleaned != key:
        return f"{cleaned}-{stable_hash(key)[:8]}"
    return cleaned


def _compact(instant: datetime) -> str:
    return instant.astimezone(UTC).strftime(_COMPACT_TIME)


def _snapshot_filename(as_of: datetime, snapshot_id: str) -> str:
    return f"{_compact(as_of)}-{snapshot_id[:12]}.json"


def _write_json(path: Path, payload: Any, *, overwrite: bool = False) -> None:
    """Write JSON atomically.

    Atomic because a half-written snapshot is indistinguishable from a
    truncated one, and both would be read back as evidence.
    """
    if path.exists() and not overwrite:
        raise SnapshotIntegrityError(f"refusing to overwrite immutable artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RepositoryError(f"corrupt stored artifact {path}: {exc}") from exc
