"""Persistence for reconciliation results.

The same discipline as every other store here, plus one property this one needs
more than the others: **running reconciliation twice against unchanged state
must not manufacture a second history.**

Ids are content-derived, so an identical comparison lands on an identical id.
When it does, the stored record is left exactly as it was and the repeat is
recorded as a re-observation in the index — the same architecture the data
layer uses for a re-collected snapshot, and for the same reason: the second
observation added no information, but the fact that we looked again is worth
keeping.

Historical reconciliations are never overwritten. A comparison is evidence
about a moment, and a moment does not change because a later one disagreed.
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

from trading_system.reconciliation.models import ReconciliationEvent, ReconciliationResult

__all__ = [
    "FilesystemReconciliationRepository",
    "ReconciliationHistoryEntry",
    "ReconciliationRepository",
    "ReconciliationStoreError",
]

_COMPACT_TIME = "%Y%m%dT%H%M%S%fZ"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class ReconciliationStoreError(RuntimeError):
    """A reconciliation could not be stored or read back."""


@dataclass(frozen=True, slots=True)
class ReconciliationHistoryEntry:
    """One line of the append-only reconciliation index."""

    reconciliation_id: str
    campaign_id: str
    as_of: datetime
    observed_at: datetime
    status: str
    broker: str
    account_reference: str
    findings: int = 0
    mismatches: int = 0
    critical: int = 0
    orders_submitted: int = 0
    content_hash: str = ""
    #: True when this line records comparing identical state again.
    reobserved: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "reconciliation_id": self.reconciliation_id,
            "campaign_id": self.campaign_id,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
            "status": self.status,
            "broker": self.broker,
            "account_reference": self.account_reference,
            "findings": self.findings,
            "mismatches": self.mismatches,
            "critical": self.critical,
            "orders_submitted": self.orders_submitted,
            "content_hash": self.content_hash,
            "reobserved": self.reobserved,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ReconciliationHistoryEntry:
        return cls(
            reconciliation_id=str(payload["reconciliation_id"]),
            campaign_id=str(payload["campaign_id"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            observed_at=datetime.fromisoformat(str(payload["observed_at"])),
            status=str(payload["status"]),
            broker=str(payload["broker"]),
            account_reference=str(payload["account_reference"]),
            findings=int(payload.get("findings", 0)),
            mismatches=int(payload.get("mismatches", 0)),
            critical=int(payload.get("critical", 0)),
            orders_submitted=int(payload.get("orders_submitted", 0)),
            content_hash=str(payload.get("content_hash", "")),
            reobserved=bool(payload.get("reobserved", False)),
        )

    @classmethod
    def of(
        cls, result: ReconciliationResult, *, reobserved: bool = False
    ) -> ReconciliationHistoryEntry:
        return cls(
            reconciliation_id=result.reconciliation_id,
            campaign_id=result.campaign_id,
            as_of=result.as_of,
            observed_at=result.observed_at,
            status=result.status.value,
            broker=result.broker,
            account_reference=result.account_reference,
            findings=len(result.findings),
            mismatches=result.counts.mismatches,
            critical=result.counts.critical,
            orders_submitted=result.orders_submitted,
            content_hash=result.content_hash,
            reobserved=reobserved,
        )


class ReconciliationRepository(ABC):
    """Storage interface for reconciliation results and their history."""

    @abstractmethod
    def save(self, result: ReconciliationResult) -> tuple[str, bool]:
        """Persist a result immutably. Returns its storage id and whether it was new."""

    @abstractmethod
    def append_event(self, event: ReconciliationEvent) -> bool: ...

    @abstractmethod
    def get(self, reconciliation_id: str) -> ReconciliationResult | None: ...

    @abstractmethod
    def latest(self) -> ReconciliationResult | None: ...

    @abstractmethod
    def events(self, reconciliation_id: str) -> list[ReconciliationEvent]: ...

    @abstractmethod
    def history(self, limit: int | None = None) -> list[ReconciliationHistoryEntry]: ...


class FilesystemReconciliationRepository(ReconciliationRepository):
    """JSON-on-disk store, rooted at ``<data>/reconciliation/``."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def results_dir(self) -> Path:
        return self._root / "results"

    @property
    def events_dir(self) -> Path:
        return self._root / "events"

    @property
    def history_path(self) -> Path:
        return self._root / "history.jsonl"

    def event_path(self, reconciliation_id: str) -> Path:
        return self.events_dir / f"{_SAFE.sub('_', reconciliation_id)}.jsonl"

    def save(self, result: ReconciliationResult) -> tuple[str, bool]:
        path = self.results_dir / _filename(result.as_of, result.reconciliation_id)
        payload = result.model_dump(mode="json")
        if path.exists():
            stored = _read_json(path)
            if stored.get("content_hash") != result.content_hash:
                raise ReconciliationStoreError(
                    f"refusing to overwrite reconciliation {result.reconciliation_id}: a stored "
                    f"comparison is immutable and the existing content differs from the new one"
                )
            _append_line(
                self.history_path, ReconciliationHistoryEntry.of(result, reobserved=True).to_json()
            )
            return str(path.relative_to(self._root)), False

        _write_json(path, payload)
        _append_line(self.history_path, ReconciliationHistoryEntry.of(result).to_json())
        return str(path.relative_to(self._root)), True

    def append_event(self, event: ReconciliationEvent) -> bool:
        existing = {stored.event_id for stored in self.events(event.reconciliation_id)}
        if event.event_id in existing:
            return False
        _append_line(self.event_path(event.reconciliation_id), event.model_dump(mode="json"))
        return True

    def get(self, reconciliation_id: str) -> ReconciliationResult | None:
        for entry in self.history():
            if entry.reconciliation_id == reconciliation_id:
                return self._load(entry.as_of, entry.reconciliation_id)
        return None

    def latest(self) -> ReconciliationResult | None:
        entries = self.history()
        if not entries:
            return None
        return self._load(entries[0].as_of, entries[0].reconciliation_id)

    def events(self, reconciliation_id: str) -> list[ReconciliationEvent]:
        payloads = _read_lines(self.event_path(reconciliation_id), f"{reconciliation_id} events")
        events = [ReconciliationEvent.model_validate(payload) for payload in payloads]
        events.sort(key=lambda event: (event.sequence, event.observed_at))
        return events

    def history(self, limit: int | None = None) -> list[ReconciliationHistoryEntry]:
        entries = [
            ReconciliationHistoryEntry.from_json(payload)
            for payload in _read_lines(self.history_path, "reconciliation history")
        ]
        entries.sort(key=lambda e: (e.observed_at, e.reconciliation_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def _load(self, as_of: datetime, reconciliation_id: str) -> ReconciliationResult:
        path = self.results_dir / _filename(as_of, reconciliation_id)
        if not path.exists():
            raise ReconciliationStoreError(f"reconciliation file is missing: {path}")
        try:
            return ReconciliationResult.model_validate(_read_json(path))
        except ReconciliationStoreError:
            raise
        except Exception as exc:
            raise ReconciliationStoreError(
                f"stored reconciliation {reconciliation_id} does not match the current model "
                f"(schema drift): {exc}"
            ) from exc


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
            raise ReconciliationStoreError(f"corrupt {label} line {path}:{number}: {exc}") from exc
    return payloads


def _write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically. A half-written comparison would still be read as one."""
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
        raise ReconciliationStoreError(f"corrupt stored reconciliation {path}: {exc}") from exc
