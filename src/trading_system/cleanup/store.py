"""Persistence for orphan-cleanup requests and runs.

The same discipline as every other store here, with one property that matters
more in this package than in any other: **a cleanup record is never
overwritten.** The record of an order that sold something out of a real account
is the only account of that act there will ever be, and a second run reaching a
different answer is a different fact rather than a correction of the first.

So: ids are content-derived, a re-run over unchanged evidence reaching the same
conclusion lands on the same id and is recorded as a re-observation, and a run
whose content differs under an id already on disk raises rather than replacing
it. The request is stored separately from the run because it has a different
lifetime — one authorisation can legitimately be observed by several runs (the
first submits, the second finds the holdings already gone).

Layout, under ``<data>/cleanup/``:

.. code-block:: text

    requests/<stamp>-<request_id>.json      what an operator authorised
    runs/<stamp>-<run_id>.json              what happened
    history.jsonl                           append-only index of runs
    requests.jsonl                          append-only index of requests
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

from trading_system.cleanup.models import OrphanCleanupRequest, OrphanCleanupRun

__all__ = [
    "CleanupHistoryEntry",
    "CleanupRepository",
    "CleanupRequestEntry",
    "CleanupStoreError",
    "FilesystemCleanupRepository",
]

_COMPACT_TIME = "%Y%m%dT%H%M%S%fZ"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class CleanupStoreError(RuntimeError):
    """A cleanup artifact could not be stored or read back."""


@dataclass(frozen=True, slots=True)
class CleanupRequestEntry:
    """One line of the append-only request index."""

    cleanup_request_id: str
    source_reconciliation_id: str
    account_reference: str
    requested_at: datetime
    targets: int
    trading_mode: str
    requested_by: str

    def to_json(self) -> dict[str, Any]:
        return {
            "cleanup_request_id": self.cleanup_request_id,
            "source_reconciliation_id": self.source_reconciliation_id,
            "account_reference": self.account_reference,
            "requested_at": self.requested_at.astimezone(UTC).isoformat(),
            "targets": self.targets,
            "trading_mode": self.trading_mode,
            "requested_by": self.requested_by,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> CleanupRequestEntry:
        return cls(
            cleanup_request_id=str(payload["cleanup_request_id"]),
            source_reconciliation_id=str(payload["source_reconciliation_id"]),
            account_reference=str(payload["account_reference"]),
            requested_at=datetime.fromisoformat(str(payload["requested_at"])),
            targets=int(payload.get("targets", 0)),
            trading_mode=str(payload["trading_mode"]),
            requested_by=str(payload.get("requested_by", "cli")),
        )

    @classmethod
    def of(cls, request: OrphanCleanupRequest) -> CleanupRequestEntry:
        return cls(
            cleanup_request_id=request.cleanup_request_id,
            source_reconciliation_id=request.source_reconciliation_id,
            account_reference=request.account_reference,
            requested_at=request.requested_at,
            targets=len(request.targets),
            trading_mode=request.trading_mode.value,
            requested_by=request.requested_by,
        )


@dataclass(frozen=True, slots=True)
class CleanupHistoryEntry:
    """One line of the append-only run index."""

    run_id: str
    cleanup_request_id: str
    source_reconciliation_id: str
    account_reference: str
    as_of: datetime
    generated_at: datetime
    status: str
    trading_mode: str
    dry_run: bool
    targets: int
    closed: int
    orders_submitted: int
    #: True when this line records running over unchanged state again.
    reobserved: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "cleanup_request_id": self.cleanup_request_id,
            "source_reconciliation_id": self.source_reconciliation_id,
            "account_reference": self.account_reference,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "generated_at": self.generated_at.astimezone(UTC).isoformat(),
            "status": self.status,
            "trading_mode": self.trading_mode,
            "dry_run": self.dry_run,
            "targets": self.targets,
            "closed": self.closed,
            "orders_submitted": self.orders_submitted,
            "reobserved": self.reobserved,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> CleanupHistoryEntry:
        return cls(
            run_id=str(payload["run_id"]),
            cleanup_request_id=str(payload["cleanup_request_id"]),
            source_reconciliation_id=str(payload["source_reconciliation_id"]),
            account_reference=str(payload["account_reference"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            generated_at=datetime.fromisoformat(str(payload["generated_at"])),
            status=str(payload["status"]),
            trading_mode=str(payload["trading_mode"]),
            dry_run=bool(payload.get("dry_run", False)),
            targets=int(payload.get("targets", 0)),
            closed=int(payload.get("closed", 0)),
            orders_submitted=int(payload.get("orders_submitted", 0)),
            reobserved=bool(payload.get("reobserved", False)),
        )

    @classmethod
    def of(cls, run: OrphanCleanupRun, *, reobserved: bool = False) -> CleanupHistoryEntry:
        return cls(
            run_id=run.run_id,
            cleanup_request_id=run.cleanup_request_id,
            source_reconciliation_id=run.source_reconciliation_id,
            account_reference=run.account_reference,
            as_of=run.as_of,
            generated_at=run.generated_at,
            status=run.status.value,
            trading_mode=run.trading_mode.value,
            dry_run=run.dry_run,
            targets=len(run.outcomes),
            closed=run.closed,
            orders_submitted=run.orders_submitted,
            reobserved=reobserved,
        )


class CleanupRepository(ABC):
    """Storage interface for cleanup requests and runs."""

    @abstractmethod
    def save_request(self, request: OrphanCleanupRequest) -> tuple[str, bool]: ...

    @abstractmethod
    def save_run(self, run: OrphanCleanupRun) -> tuple[str, bool]: ...

    @abstractmethod
    def get_request(self, cleanup_request_id: str) -> OrphanCleanupRequest | None: ...

    @abstractmethod
    def get_run(self, run_id: str) -> OrphanCleanupRun | None: ...

    @abstractmethod
    def latest_run(self) -> OrphanCleanupRun | None: ...

    @abstractmethod
    def history(self, limit: int | None = None) -> list[CleanupHistoryEntry]: ...

    @abstractmethod
    def requests(self, limit: int | None = None) -> list[CleanupRequestEntry]: ...


class FilesystemCleanupRepository(CleanupRepository):
    """JSON-on-disk store, rooted at ``<data>/cleanup/``."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def runs_dir(self) -> Path:
        return self._root / "runs"

    @property
    def requests_dir(self) -> Path:
        return self._root / "requests"

    @property
    def history_path(self) -> Path:
        return self._root / "history.jsonl"

    @property
    def requests_path(self) -> Path:
        return self._root / "requests.jsonl"

    def save_request(self, request: OrphanCleanupRequest) -> tuple[str, bool]:
        path = self.requests_dir / _filename(request.requested_at, request.cleanup_request_id)
        payload = request.model_dump(mode="json")
        if path.exists():
            stored = _read_json(path)
            if _digest(stored) != _digest(payload):
                raise CleanupStoreError(
                    f"refusing to overwrite cleanup request {request.cleanup_request_id}: a "
                    f"stored authorisation is immutable and the existing content differs"
                )
            _append_line(self.requests_path, CleanupRequestEntry.of(request).to_json())
            return str(path.relative_to(self._root)), False

        _write_json(path, payload)
        _append_line(self.requests_path, CleanupRequestEntry.of(request).to_json())
        return str(path.relative_to(self._root)), True

    def save_run(self, run: OrphanCleanupRun) -> tuple[str, bool]:
        path = self.runs_dir / _filename(run.as_of, run.run_id)
        payload = run.model_dump(mode="json")
        if path.exists():
            stored = _read_json(path)
            if _comparable(stored) != _comparable(payload):
                raise CleanupStoreError(
                    f"refusing to overwrite cleanup run {run.run_id}: the stored record of an "
                    f"order that sold out of a real account is immutable, and the existing "
                    f"content differs from the new one"
                )
            _append_line(self.history_path, CleanupHistoryEntry.of(run, reobserved=True).to_json())
            return str(path.relative_to(self._root)), False

        _write_json(path, payload)
        _append_line(self.history_path, CleanupHistoryEntry.of(run).to_json())
        return str(path.relative_to(self._root)), True

    def get_request(self, cleanup_request_id: str) -> OrphanCleanupRequest | None:
        for entry in self.requests():
            if entry.cleanup_request_id == cleanup_request_id:
                path = self.requests_dir / _filename(entry.requested_at, entry.cleanup_request_id)
                if not path.exists():
                    raise CleanupStoreError(f"cleanup request file is missing: {path}")
                return _validate_request(_read_json(path), cleanup_request_id)
        return None

    def get_run(self, run_id: str) -> OrphanCleanupRun | None:
        for entry in self.history():
            if entry.run_id == run_id:
                return self._load(entry)
        return None

    def latest_run(self) -> OrphanCleanupRun | None:
        entries = self.history()
        return self._load(entries[0]) if entries else None

    def history(self, limit: int | None = None) -> list[CleanupHistoryEntry]:
        entries = [
            CleanupHistoryEntry.from_json(payload)
            for payload in _read_lines(self.history_path, "cleanup history")
        ]
        entries.sort(key=lambda entry: (entry.generated_at, entry.run_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def requests(self, limit: int | None = None) -> list[CleanupRequestEntry]:
        entries = [
            CleanupRequestEntry.from_json(payload)
            for payload in _read_lines(self.requests_path, "cleanup requests")
        ]
        entries.sort(key=lambda entry: (entry.requested_at, entry.cleanup_request_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def _load(self, entry: CleanupHistoryEntry) -> OrphanCleanupRun:
        path = self.runs_dir / _filename(entry.as_of, entry.run_id)
        if not path.exists():
            raise CleanupStoreError(f"cleanup run file is missing: {path}")
        return _validate_run(_read_json(path), entry.run_id)


def _validate_request(payload: Any, identifier: str) -> OrphanCleanupRequest:
    try:
        return OrphanCleanupRequest.model_validate(payload)
    except Exception as exc:
        raise CleanupStoreError(_DRIFT.format(identifier=identifier, error=exc)) from exc


def _validate_run(payload: Any, identifier: str) -> OrphanCleanupRun:
    try:
        return OrphanCleanupRun.model_validate(payload)
    except Exception as exc:
        raise CleanupStoreError(_DRIFT.format(identifier=identifier, error=exc)) from exc


_DRIFT = (
    "stored cleanup artifact {identifier} does not match the current model (schema drift): {error}"
)


def _digest(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


#: Fields that record *when this process looked*, not *what it found*. Excluded
#: from the immutability comparison for the same reason the data layer's payload
#: hash excludes observation clocks: a run that reached an identical conclusion
#: about identical evidence is the same fact observed twice, and comparing the
#: wall clock would make every re-run look like a contradiction and raise.
_OBSERVATION_CLOCKS = ("generated_at", "trace_id")
_OUTCOME_OBSERVATION_CLOCKS = ("observed_after_at",)


def _comparable(payload: Any) -> str:
    if not isinstance(payload, dict):
        return _digest(payload)
    stripped = {key: value for key, value in payload.items() if key not in _OBSERVATION_CLOCKS}
    outcomes = stripped.get("outcomes")
    if isinstance(outcomes, list):
        stripped["outcomes"] = [
            {key: value for key, value in outcome.items() if key not in _OUTCOME_OBSERVATION_CLOCKS}
            if isinstance(outcome, dict)
            else outcome
            for outcome in outcomes
        ]
    return _digest(stripped)


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
            raise CleanupStoreError(f"corrupt {label} line {path}:{number}: {exc}") from exc
    return payloads


def _write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically. A half-written cleanup record would still read as one."""
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
        raise CleanupStoreError(f"corrupt stored cleanup artifact {path}: {exc}") from exc
