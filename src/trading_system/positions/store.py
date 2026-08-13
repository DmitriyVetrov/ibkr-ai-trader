"""Persistence for broker position snapshots and recorded fills.

The same discipline as every other store in this system, and the same reasons:

* **Immutable.** A snapshot file and a fill file are written once. Writing
  different content under an existing id raises rather than overwriting.
* **Append-only index.** ``history.jsonl`` gains a line per record and is never
  rewritten.
* **Atomic.** Written through a temporary file and ``os.replace``: a
  half-written snapshot is indistinguishable from a truncated one, and both
  would be read back as an account state.
* **Content-addressed.** Ids derive from what the broker said, so re-reading an
  unchanged account or re-observing the same fill records a *re-observation*
  rather than a second, differently-named copy of one fact.

That last property is what makes running reconciliation twice safe. Polling the
broker again sees the same fills and the same holdings; the second run adds no
fill, no position and no economic change, and says so.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_system.positions.models import (
    BrokerPositionSnapshot,
    ObservedFill,
    ObservedPosition,
)

__all__ = [
    "FilesystemFillRepository",
    "FilesystemPositionRepository",
    "FillHistoryEntry",
    "FillRepository",
    "PositionHistoryEntry",
    "PositionRepository",
    "PositionStoreError",
]

_COMPACT_TIME = "%Y%m%dT%H%M%S%fZ"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class PositionStoreError(RuntimeError):
    """A position snapshot or fill could not be stored or read back."""


@dataclass(frozen=True, slots=True)
class PositionHistoryEntry:
    """One line of the append-only snapshot index."""

    snapshot_id: str
    as_of: datetime
    observed_at: datetime
    broker: str
    account_reference: str
    read_status: str
    positions: int = 0
    content_hash: str = ""
    trading_mode: str = ""
    #: True when this line records seeing content already on file, rather than
    #: a new account state. Keeps a re-poll visible without duplicating history.
    reobserved: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
            "broker": self.broker,
            "account_reference": self.account_reference,
            "read_status": self.read_status,
            "positions": self.positions,
            "content_hash": self.content_hash,
            "trading_mode": self.trading_mode,
            "reobserved": self.reobserved,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> PositionHistoryEntry:
        return cls(
            snapshot_id=str(payload["snapshot_id"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            observed_at=datetime.fromisoformat(str(payload["observed_at"])),
            broker=str(payload["broker"]),
            account_reference=str(payload["account_reference"]),
            read_status=str(payload["read_status"]),
            positions=int(payload.get("positions", 0)),
            content_hash=str(payload.get("content_hash", "")),
            trading_mode=str(payload.get("trading_mode", "")),
            reobserved=bool(payload.get("reobserved", False)),
        )

    @classmethod
    def of(
        cls, snapshot: BrokerPositionSnapshot, *, reobserved: bool = False
    ) -> PositionHistoryEntry:
        return cls(
            snapshot_id=snapshot.snapshot_id,
            as_of=snapshot.as_of,
            observed_at=snapshot.observed_at,
            broker=snapshot.broker,
            account_reference=snapshot.account_reference,
            read_status=snapshot.read_status.value,
            positions=len(snapshot.positions),
            content_hash=snapshot.content_hash,
            trading_mode=snapshot.trading_mode.value,
            reobserved=reobserved,
        )


@dataclass(frozen=True, slots=True)
class FillHistoryEntry:
    """One line of the append-only fill index."""

    fill_id: str
    executed_at: datetime
    observed_at: datetime
    key: str
    underlying: str
    side: str
    quantity: str
    price: str
    broker_execution_id: str | None = None
    broker_order_id: str | None = None
    execution_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "executed_at": self.executed_at.astimezone(UTC).isoformat(),
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
            "key": self.key,
            "underlying": self.underlying,
            "side": self.side,
            "quantity": self.quantity,
            "price": self.price,
            "broker_execution_id": self.broker_execution_id,
            "broker_order_id": self.broker_order_id,
            "execution_id": self.execution_id,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> FillHistoryEntry:
        return cls(
            fill_id=str(payload["fill_id"]),
            executed_at=datetime.fromisoformat(str(payload["executed_at"])),
            observed_at=datetime.fromisoformat(str(payload["observed_at"])),
            key=str(payload["key"]),
            underlying=str(payload["underlying"]),
            side=str(payload["side"]),
            quantity=str(payload["quantity"]),
            price=str(payload["price"]),
            broker_execution_id=payload.get("broker_execution_id"),
            broker_order_id=payload.get("broker_order_id"),
            execution_id=payload.get("execution_id"),
        )

    @classmethod
    def of(cls, fill: ObservedFill) -> FillHistoryEntry:
        return cls(
            fill_id=fill.fill_id,
            executed_at=fill.executed_at,
            observed_at=fill.observed_at,
            key=fill.key,
            underlying=fill.underlying,
            side=fill.side.value,
            quantity=str(fill.quantity),
            price=str(fill.price),
            broker_execution_id=fill.broker_execution_id,
            broker_order_id=fill.broker_order_id,
            execution_id=fill.execution_id,
        )


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------
class PositionRepository(ABC):
    """Storage interface for what the broker reported holding."""

    @abstractmethod
    def save_snapshot(self, snapshot: BrokerPositionSnapshot) -> str:
        """Persist a snapshot immutably. Returns its storage id."""

    @abstractmethod
    def get_snapshot(self, snapshot_id: str) -> BrokerPositionSnapshot | None: ...

    @abstractmethod
    def latest(self) -> BrokerPositionSnapshot | None: ...

    @abstractmethod
    def latest_as_of(self, instant: datetime) -> BrokerPositionSnapshot | None:
        """The newest snapshot already observed at ``instant``.

        Retrieval binds: a capture taken after ``instant`` was not available
        then, however recent the holdings it describes.
        """

    @abstractmethod
    def latest_usable(self) -> BrokerPositionSnapshot | None:
        """The newest snapshot the broker actually answered.

        Separate from :meth:`latest` on purpose. A failed read is stored — the
        attempt is part of the record — and a consumer that reconciled against
        it would compare the internal ledger with an absence of data.
        """

    @abstractmethod
    def history(self, limit: int | None = None) -> list[PositionHistoryEntry]: ...

    @abstractmethod
    def by_contract(self, key: str, limit: int | None = None) -> list[ObservedPosition]:
        """Every observation of one instrument, newest first."""

    @abstractmethod
    def by_underlying(self, underlying: str, limit: int | None = None) -> list[ObservedPosition]:
        """Every observation of any contract on one underlying, newest first."""

    @abstractmethod
    def reconstruct(self, instant: datetime) -> BrokerPositionSnapshot | None:
        """What the broker was known to hold at ``instant``."""


class FillRepository(ABC):
    """Storage interface for recorded broker fills."""

    @abstractmethod
    def save(self, fill: ObservedFill) -> tuple[str, bool]:
        """Persist one fill. Returns its storage id and whether it was new."""

    @abstractmethod
    def save_many(
        self, fills: Sequence[ObservedFill]
    ) -> tuple[list[ObservedFill], list[ObservedFill]]:
        """Persist many, returning ``(newly recorded, already known)``."""

    @abstractmethod
    def get(self, fill_id: str) -> ObservedFill | None: ...

    @abstractmethod
    def all(self, limit: int | None = None) -> list[ObservedFill]: ...

    @abstractmethod
    def known_ids(self) -> set[str]: ...

    @abstractmethod
    def for_execution(self, execution_id: str) -> list[ObservedFill]: ...

    @abstractmethod
    def for_contract(self, key: str) -> list[ObservedFill]: ...

    @abstractmethod
    def history(self, limit: int | None = None) -> list[FillHistoryEntry]: ...


# ---------------------------------------------------------------------------
# Filesystem implementations
# ---------------------------------------------------------------------------
class FilesystemPositionRepository(PositionRepository):
    """JSON-on-disk store, rooted at ``<data>/positions/``."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def snapshots_dir(self) -> Path:
        return self._root / "snapshots"

    @property
    def history_path(self) -> Path:
        return self._root / "history.jsonl"

    def save_snapshot(self, snapshot: BrokerPositionSnapshot) -> str:
        path = self.snapshots_dir / _filename(snapshot.as_of, snapshot.snapshot_id)
        payload = snapshot.model_dump(mode="json")
        if path.exists():
            stored = _read_json(path)
            if stored.get("content_hash") != snapshot.content_hash:
                raise PositionStoreError(
                    f"refusing to overwrite position snapshot {snapshot.snapshot_id}: a stored "
                    f"snapshot is immutable and the existing content differs from the new one"
                )
            # Same holdings, seen again. Recorded as a re-observation so the
            # ledger shows that we looked, without claiming a second account
            # state that never existed.
            _append_line(
                self.history_path, PositionHistoryEntry.of(snapshot, reobserved=True).to_json()
            )
            return str(path.relative_to(self._root))

        _write_json(path, payload)
        _append_line(self.history_path, PositionHistoryEntry.of(snapshot).to_json())
        return str(path.relative_to(self._root))

    def get_snapshot(self, snapshot_id: str) -> BrokerPositionSnapshot | None:
        for entry in self.history():
            if entry.snapshot_id == snapshot_id:
                return self._load(entry.as_of, entry.snapshot_id)
        return None

    def latest(self) -> BrokerPositionSnapshot | None:
        entries = self.history()
        if not entries:
            return None
        return self._load(entries[0].as_of, entries[0].snapshot_id)

    def latest_as_of(self, instant: datetime) -> BrokerPositionSnapshot | None:
        visible = [entry for entry in self.history() if entry.observed_at <= instant]
        if not visible:
            return None
        newest = max(visible, key=lambda e: (e.as_of, e.observed_at, e.snapshot_id))
        return self._load(newest.as_of, newest.snapshot_id)

    def latest_usable(self) -> BrokerPositionSnapshot | None:
        for entry in self.history():
            snapshot = self._load(entry.as_of, entry.snapshot_id)
            if snapshot.usable:
                return snapshot
        return None

    def history(self, limit: int | None = None) -> list[PositionHistoryEntry]:
        entries = [
            PositionHistoryEntry.from_json(payload)
            for payload in _read_lines(self.history_path, "position history")
        ]
        entries.sort(key=lambda e: (e.observed_at, e.as_of, e.snapshot_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def by_contract(self, key: str, limit: int | None = None) -> list[ObservedPosition]:
        found: list[ObservedPosition] = []
        for entry in self.history():
            if entry.reobserved:
                continue
            snapshot = self._load(entry.as_of, entry.snapshot_id)
            position = snapshot.by_key(key)
            if position is not None:
                found.append(position)
            if limit is not None and len(found) >= limit:
                break
        return found

    def by_underlying(self, underlying: str, limit: int | None = None) -> list[ObservedPosition]:
        wanted = underlying.strip().upper()
        found: list[ObservedPosition] = []
        for entry in self.history():
            if entry.reobserved:
                continue
            snapshot = self._load(entry.as_of, entry.snapshot_id)
            found.extend(snapshot.for_underlying(wanted))
            if limit is not None and len(found) >= limit:
                return found[:limit]
        return found

    def reconstruct(self, instant: datetime) -> BrokerPositionSnapshot | None:
        return self.latest_as_of(instant)

    def _load(self, as_of: datetime, snapshot_id: str) -> BrokerPositionSnapshot:
        path = self.snapshots_dir / _filename(as_of, snapshot_id)
        if not path.exists():
            raise PositionStoreError(f"position snapshot file is missing: {path}")
        try:
            return BrokerPositionSnapshot.model_validate(_read_json(path))
        except PositionStoreError:
            raise
        except Exception as exc:
            raise PositionStoreError(
                f"stored position snapshot {snapshot_id} does not match the current model "
                f"(schema drift): {exc}"
            ) from exc


class FilesystemFillRepository(FillRepository):
    """JSON-on-disk store, rooted at ``<data>/fills/``."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def fills_dir(self) -> Path:
        return self._root / "fills"

    @property
    def history_path(self) -> Path:
        return self._root / "history.jsonl"

    def save(self, fill: ObservedFill) -> tuple[str, bool]:
        path = self.fills_dir / _filename(fill.executed_at, fill.fill_id)
        payload = fill.model_dump(mode="json")
        if path.exists():
            stored = _read_json(path)
            if _economic(stored) != _economic(payload):
                raise PositionStoreError(
                    f"refusing to overwrite fill {fill.fill_id}: a recorded fill is immutable "
                    f"and the broker's own report of it has changed. One of the two readings "
                    f"is wrong, and silently keeping either would hide which"
                )
            # Re-observed. The same trade, seen again — no second fill.
            return str(path.relative_to(self._root)), False

        _write_json(path, payload)
        _append_line(self.history_path, FillHistoryEntry.of(fill).to_json())
        return str(path.relative_to(self._root)), True

    def save_many(
        self, fills: Sequence[ObservedFill]
    ) -> tuple[list[ObservedFill], list[ObservedFill]]:
        stored: list[ObservedFill] = []
        known: list[ObservedFill] = []
        for fill in fills:
            _, is_new = self.save(fill)
            (stored if is_new else known).append(fill)
        return stored, known

    def get(self, fill_id: str) -> ObservedFill | None:
        for entry in self.history():
            if entry.fill_id == fill_id:
                return self._load(entry.executed_at, entry.fill_id)
        return None

    def all(self, limit: int | None = None) -> list[ObservedFill]:
        fills = [self._load(entry.executed_at, entry.fill_id) for entry in self.history(limit)]
        return sorted(fills, key=lambda fill: (fill.executed_at, fill.fill_id))

    def known_ids(self) -> set[str]:
        return {entry.fill_id for entry in self.history()}

    def for_execution(self, execution_id: str) -> list[ObservedFill]:
        return [
            self._load(entry.executed_at, entry.fill_id)
            for entry in sorted(self.history(), key=lambda e: (e.executed_at, e.fill_id))
            if entry.execution_id == execution_id
        ]

    def for_contract(self, key: str) -> list[ObservedFill]:
        return [
            self._load(entry.executed_at, entry.fill_id)
            for entry in sorted(self.history(), key=lambda e: (e.executed_at, e.fill_id))
            if entry.key == key
        ]

    def history(self, limit: int | None = None) -> list[FillHistoryEntry]:
        entries = [
            FillHistoryEntry.from_json(payload)
            for payload in _read_lines(self.history_path, "fill history")
        ]
        entries.sort(key=lambda e: (e.executed_at, e.fill_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def _load(self, executed_at: datetime, fill_id: str) -> ObservedFill:
        path = self.fills_dir / _filename(executed_at, fill_id)
        if not path.exists():
            raise PositionStoreError(f"fill file is missing: {path}")
        try:
            return ObservedFill.model_validate(_read_json(path))
        except PositionStoreError:
            raise
        except Exception as exc:
            raise PositionStoreError(
                f"stored fill {fill_id} does not match the current model (schema drift): {exc}"
            ) from exc


#: Fields of a stored fill that describe the *trade* rather than our sight of it.
#:
#: Re-observing a fill through a different code path can legitimately change
#: ``observed_at`` or which execution we have since linked it to. It can never
#: change what traded, so only these are compared before accepting a repeat.
_ECONOMIC_FIELDS = (
    "broker_execution_id",
    "broker_order_id",
    "key",
    "side",
    "quantity",
    "price",
    "commission",
    "currency",
    "executed_at",
)


def _economic(payload: dict[str, Any]) -> dict[str, Any]:
    return {name: payload.get(name) for name in _ECONOMIC_FIELDS}


def _filename(stamp: datetime, identifier: str) -> str:
    return f"{stamp.astimezone(UTC).strftime(_COMPACT_TIME)}-{_SAFE.sub('_', identifier)}.json"


def _append_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _read_lines(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payloads: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payloads.append(json.loads(line))
        except Exception as exc:
            raise PositionStoreError(f"corrupt {label} line {path}:{number}: {exc}") from exc
    return payloads


def _write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically. A half-written record would still be read as history."""
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
        raise PositionStoreError(f"corrupt stored position record {path}: {exc}") from exc
