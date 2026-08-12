"""Persistence for account snapshots.

Same discipline as every other store in this system, for the same reason: an
authorisation made on 12 August must stay explainable in November, when the
account holds different money and the broker has forgotten what it said.

* **Immutable.** A snapshot file is written once. Writing different content to
  an existing id raises rather than overwriting.
* **Append-only index.** ``history.jsonl`` gains a line per capture and is
  never rewritten.
* **Atomic.** Written through a temporary file and ``os.replace``, because a
  half-written snapshot is indistinguishable from a truncated one and both
  would be read back as an account balance.

Snapshot ids are content-derived, so capturing an unchanged account twice
returns the existing record instead of writing a second, differently-named copy
of the same balance.

The abstract repository is what consumers see; the filesystem is one
implementation, and nothing outside this module handles a path.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_system.risk.models import AccountSnapshot

__all__ = [
    "AccountSnapshotEntry",
    "AccountSnapshotRepository",
    "FilesystemAccountSnapshotRepository",
    "RiskStoreError",
]

_COMPACT_TIME = "%Y%m%dT%H%M%S%fZ"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class RiskStoreError(RuntimeError):
    """An account snapshot could not be stored or read back."""


@dataclass(frozen=True, slots=True)
class AccountSnapshotEntry:
    """One line of the append-only capture history."""

    snapshot_id: str
    as_of: datetime
    captured_at: datetime
    broker: str
    account_id: str
    currency: str
    trading_mode: str
    positions: int = 0
    simulated: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "captured_at": self.captured_at.astimezone(UTC).isoformat(),
            "broker": self.broker,
            "account_id": self.account_id,
            "currency": self.currency,
            "trading_mode": self.trading_mode,
            "positions": self.positions,
            "simulated": self.simulated,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> AccountSnapshotEntry:
        return cls(
            snapshot_id=str(payload["snapshot_id"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            captured_at=datetime.fromisoformat(str(payload["captured_at"])),
            broker=str(payload["broker"]),
            account_id=str(payload["account_id"]),
            currency=str(payload["currency"]),
            trading_mode=str(payload["trading_mode"]),
            positions=int(payload.get("positions", 0)),
            simulated=bool(payload.get("simulated", False)),
        )

    @classmethod
    def of(cls, snapshot: AccountSnapshot) -> AccountSnapshotEntry:
        return cls(
            snapshot_id=snapshot.snapshot_id,
            as_of=snapshot.as_of,
            captured_at=snapshot.captured_at,
            broker=snapshot.broker,
            account_id=snapshot.account_id,
            currency=snapshot.currency,
            trading_mode=snapshot.trading_mode.value,
            positions=len(snapshot.positions),
            simulated=snapshot.simulated,
        )


class AccountSnapshotRepository(ABC):
    """Storage interface for captured account state."""

    @abstractmethod
    def save(self, snapshot: AccountSnapshot) -> str:
        """Persist a snapshot immutably. Returns its storage id."""

    @abstractmethod
    def get(self, snapshot_id: str) -> AccountSnapshot | None: ...

    @abstractmethod
    def latest(self) -> AccountSnapshot | None: ...

    @abstractmethod
    def latest_as_of(self, instant: datetime) -> AccountSnapshot | None:
        """The newest snapshot that was already knowable at ``instant``.

        Point-in-time by construction: a capture taken after ``instant`` is
        invisible here, so a historical replay cannot allocate against an
        account balance nobody had yet seen.
        """

    @abstractmethod
    def history(self, limit: int | None = None) -> list[AccountSnapshotEntry]: ...


class FilesystemAccountSnapshotRepository(AccountSnapshotRepository):
    """JSON-on-disk store, rooted at ``<data>/accounts/``."""

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

    def save(self, snapshot: AccountSnapshot) -> str:
        path = self.snapshots_dir / _snapshot_filename(snapshot.as_of, snapshot.snapshot_id)
        payload = snapshot.model_dump(mode="json")
        if path.exists():
            stored = _read_json(path)
            if stored != payload:
                raise RiskStoreError(
                    f"refusing to overwrite account snapshot {snapshot.snapshot_id}: a stored "
                    f"snapshot is immutable and the existing record differs from the new one"
                )
            return str(path.relative_to(self._root))

        _write_json(path, payload)
        _append_line(self.history_path, AccountSnapshotEntry.of(snapshot).to_json())
        return str(path.relative_to(self._root))

    def get(self, snapshot_id: str) -> AccountSnapshot | None:
        for entry in self.history():
            if entry.snapshot_id == snapshot_id:
                return self._load(entry.as_of, entry.snapshot_id)
        return None

    def latest(self) -> AccountSnapshot | None:
        entries = self.history()
        if not entries:
            return None
        return self._load(entries[0].as_of, entries[0].snapshot_id)

    def latest_as_of(self, instant: datetime) -> AccountSnapshot | None:
        visible = [entry for entry in self.history() if entry.captured_at <= instant]
        if not visible:
            return None
        newest = max(visible, key=lambda e: (e.as_of, e.captured_at, e.snapshot_id))
        return self._load(newest.as_of, newest.snapshot_id)

    def history(self, limit: int | None = None) -> list[AccountSnapshotEntry]:
        entries = [
            AccountSnapshotEntry.from_json(payload)
            for payload in _read_lines(self.history_path, "account snapshot history")
        ]
        entries.sort(key=lambda e: (e.captured_at, e.snapshot_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def _load(self, as_of: datetime, snapshot_id: str) -> AccountSnapshot:
        path = self.snapshots_dir / _snapshot_filename(as_of, snapshot_id)
        if not path.exists():
            raise RiskStoreError(f"account snapshot file is missing: {path}")
        try:
            return AccountSnapshot.model_validate(_read_json(path))
        except RiskStoreError:
            raise
        except Exception as exc:
            raise RiskStoreError(
                f"stored account snapshot {snapshot_id} does not match the current model "
                f"(schema drift): {exc}"
            ) from exc


def _snapshot_filename(as_of: datetime, snapshot_id: str) -> str:
    return f"{as_of.astimezone(UTC).strftime(_COMPACT_TIME)}-{_SAFE.sub('_', snapshot_id)}.json"


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
            raise RiskStoreError(f"corrupt {label} line {path}:{number}: {exc}") from exc
    return payloads


def _write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically. A half-written snapshot would still be read back."""
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
        raise RiskStoreError(f"corrupt stored account snapshot {path}: {exc}") from exc
