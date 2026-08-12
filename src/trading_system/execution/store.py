"""Persistence for executions.

The same discipline as the universe, research, strategy and allocation stores,
and here it carries the most weight of any of them: this is the record of what
was actually sent to a broker. If it disagrees with reality, the system will
either place a trade twice or believe it holds a position it does not.

* **Immutable base record.** An execution's file is written once and never
  rewritten. A later fill does not edit it — it appends an event, and the
  current state is *folded* from the events. That is why an order that reported
  two fills can still show that it once reported one.
* **Append-only history.** ``history.jsonl`` gains a line per execution and
  ``events/<execution_id>.jsonl`` a line per observation. Neither is ever
  rewritten.
* **Atomic.** Written through a temporary file and ``os.replace``, because a
  half-written execution record is indistinguishable from a truncated one and
  both would be read back as "we may have sent an order".
* **Written before the broker call, not after.** The service persists a record
  in ``SUBMISSION_PENDING`` *before* submitting. A process that dies during a
  submission then leaves evidence that an order may be in flight, which is the
  difference between a recoverable ambiguity and a silent duplicate.

Dry runs are never stored. A diagnostic that entered this ledger would make the
next run believe an order had been sent.
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

from trading_system.domain.enums import ExecutionState
from trading_system.execution.models import (
    ExecutionEvent,
    ExecutionRecord,
    ExecutionRunResult,
)

__all__ = [
    "ExecutionHistoryEntry",
    "ExecutionRepository",
    "ExecutionRunHistoryEntry",
    "ExecutionStoreError",
    "FilesystemExecutionRepository",
]

_COMPACT_TIME = "%Y%m%dT%H%M%S%fZ"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class ExecutionStoreError(RuntimeError):
    """An execution could not be stored or read back."""


@dataclass(frozen=True, slots=True)
class ExecutionHistoryEntry:
    """One line of the append-only execution index.

    Carries ``execution_request_id`` because that is what idempotency is
    checked against: before submitting, the service asks this index whether
    this exact trade, executed this exact way, already has an attempt on
    record.
    """

    execution_id: str
    execution_request_id: str
    allocation_id: str
    campaign_id: str
    symbol: str
    created_at: datetime
    state: str
    trading_mode: str
    broker: str
    quantity: int = 0
    broker_order_id: str | None = None
    run_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "execution_request_id": self.execution_request_id,
            "allocation_id": self.allocation_id,
            "campaign_id": self.campaign_id,
            "symbol": self.symbol,
            "created_at": self.created_at.astimezone(UTC).isoformat(),
            "state": self.state,
            "trading_mode": self.trading_mode,
            "broker": self.broker,
            "quantity": self.quantity,
            "broker_order_id": self.broker_order_id,
            "run_id": self.run_id,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ExecutionHistoryEntry:
        return cls(
            execution_id=str(payload["execution_id"]),
            execution_request_id=str(payload["execution_request_id"]),
            allocation_id=str(payload["allocation_id"]),
            campaign_id=str(payload["campaign_id"]),
            symbol=str(payload["symbol"]),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            state=str(payload["state"]),
            trading_mode=str(payload["trading_mode"]),
            broker=str(payload["broker"]),
            quantity=int(payload.get("quantity", 0)),
            broker_order_id=payload.get("broker_order_id"),
            run_id=payload.get("run_id"),
        )

    @classmethod
    def of(cls, record: ExecutionRecord, *, run_id: str | None = None) -> ExecutionHistoryEntry:
        return cls(
            execution_id=record.execution_id,
            execution_request_id=record.execution_request_id,
            allocation_id=record.allocation_id,
            campaign_id=record.campaign_id,
            symbol=record.underlying,
            created_at=record.created_at,
            state=record.state.value,
            trading_mode=record.trading_mode.value,
            broker=record.broker,
            quantity=record.quantity,
            broker_order_id=record.broker_order_id,
            run_id=run_id,
        )


@dataclass(frozen=True, slots=True)
class ExecutionRunHistoryEntry:
    """One line of the append-only execution-run history."""

    run_id: str
    campaign_id: str
    as_of: datetime
    generated_at: datetime
    status: str
    trading_mode: str
    broker: str
    submitted: int = 0
    refused: int = 0
    orders_submitted: int = 0
    allocation_run_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "campaign_id": self.campaign_id,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "generated_at": self.generated_at.astimezone(UTC).isoformat(),
            "status": self.status,
            "trading_mode": self.trading_mode,
            "broker": self.broker,
            "submitted": self.submitted,
            "refused": self.refused,
            "orders_submitted": self.orders_submitted,
            "allocation_run_id": self.allocation_run_id,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ExecutionRunHistoryEntry:
        return cls(
            run_id=str(payload["run_id"]),
            campaign_id=str(payload["campaign_id"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            generated_at=datetime.fromisoformat(str(payload["generated_at"])),
            status=str(payload["status"]),
            trading_mode=str(payload["trading_mode"]),
            broker=str(payload["broker"]),
            submitted=int(payload.get("submitted", 0)),
            refused=int(payload.get("refused", 0)),
            orders_submitted=int(payload.get("orders_submitted", 0)),
            allocation_run_id=payload.get("allocation_run_id"),
        )

    @classmethod
    def of(cls, result: ExecutionRunResult) -> ExecutionRunHistoryEntry:
        return cls(
            run_id=result.run_id,
            campaign_id=result.campaign_id,
            as_of=result.as_of,
            generated_at=result.generated_at,
            status=result.status.value,
            trading_mode=result.trading_mode.value,
            broker=result.broker,
            submitted=result.counts.submitted,
            refused=result.counts.refused,
            orders_submitted=result.orders_submitted,
            allocation_run_id=result.allocation_run_id,
        )


class ExecutionRepository(ABC):
    """Storage interface for executions and their history."""

    @abstractmethod
    def save(self, record: ExecutionRecord, *, run_id: str | None = None) -> str:
        """Persist an execution's base record immutably. Returns its storage id."""

    @abstractmethod
    def append_event(self, event: ExecutionEvent) -> None:
        """Append one observation. Never rewrites an earlier one."""

    @abstractmethod
    def base(self, execution_id: str) -> ExecutionRecord | None:
        """The record exactly as first written, before any broker news."""

    @abstractmethod
    def current(self, execution_id: str) -> ExecutionRecord | None:
        """The record with every stored event folded in, newest state last."""

    @abstractmethod
    def events(self, execution_id: str) -> list[ExecutionEvent]:
        """Every observation for one execution, oldest first."""

    @abstractmethod
    def history(self, limit: int | None = None) -> list[ExecutionHistoryEntry]:
        """The execution index, newest first."""

    @abstractmethod
    def for_request(self, execution_request_id: str) -> list[ExecutionRecord]:
        """Every attempt recorded against one execution identity.

        What idempotency is decided from: an attempt already in a state where
        an order may exist at the broker means the next request must not send
        another one.
        """

    @abstractmethod
    def for_allocation(self, allocation_id: str) -> list[ExecutionRecord]:
        """Every attempt against one authorisation, however it was parameterised."""

    @abstractmethod
    def save_run(self, result: ExecutionRunResult) -> str: ...

    @abstractmethod
    def get_run(self, run_id: str) -> ExecutionRunResult | None: ...

    @abstractmethod
    def latest_run(self) -> ExecutionRunResult | None: ...

    @abstractmethod
    def run_history(self, limit: int | None = None) -> list[ExecutionRunHistoryEntry]: ...


class FilesystemExecutionRepository(ExecutionRepository):
    """JSON-on-disk store, rooted at ``<data>/execution/``."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def executions_dir(self) -> Path:
        return self._root / "executions"

    @property
    def events_dir(self) -> Path:
        return self._root / "events"

    @property
    def runs_dir(self) -> Path:
        return self._root / "runs"

    @property
    def history_path(self) -> Path:
        return self._root / "history.jsonl"

    @property
    def run_history_path(self) -> Path:
        return self._root / "runs.jsonl"

    def event_path(self, execution_id: str) -> Path:
        return self.events_dir / f"{_SAFE.sub('_', execution_id)}.jsonl"

    # --- executions --------------------------------------------------------
    def save(self, record: ExecutionRecord, *, run_id: str | None = None) -> str:
        if record.dry_run:
            raise ExecutionStoreError(
                f"refusing to store dry run {record.execution_id}: a diagnostic record in this "
                f"ledger would make a later run believe an order had been sent"
            )
        path = self.executions_dir / _filename(record.created_at, record.execution_id)
        payload = record.model_dump(mode="json")
        if path.exists():
            stored = _read_json(path)
            if stored != payload:
                raise ExecutionStoreError(
                    f"refusing to overwrite execution {record.execution_id}: a stored execution "
                    f"is immutable, and the existing record differs from the new one. Later "
                    f"broker news is appended as an event, never written over the original"
                )
            return str(path.relative_to(self._root))

        _write_json(path, payload)
        _append_line(self.history_path, ExecutionHistoryEntry.of(record, run_id=run_id).to_json())
        return str(path.relative_to(self._root))

    def append_event(self, event: ExecutionEvent) -> None:
        existing = {stored.event_id for stored in self.events(event.execution_id)}
        if event.event_id in existing:
            # A replayed observation is not new information. Silently ignoring
            # it keeps a re-poll idempotent without pretending it never
            # happened — the original event is already on file.
            return
        _append_line(self.event_path(event.execution_id), event.model_dump(mode="json"))

    def base(self, execution_id: str) -> ExecutionRecord | None:
        for entry in self.history():
            if entry.execution_id == execution_id:
                return self._load(entry.created_at, entry.execution_id)
        return None

    def current(self, execution_id: str) -> ExecutionRecord | None:
        record = self.base(execution_id)
        if record is None:
            return None
        return self._fold(record)

    def events(self, execution_id: str) -> list[ExecutionEvent]:
        payloads = _read_lines(self.event_path(execution_id), f"{execution_id} events")
        events = [ExecutionEvent.model_validate(payload) for payload in payloads]
        # Sequence first: two observations can share an observation timestamp,
        # and the order they were recorded in is the one that happened.
        events.sort(key=lambda e: (e.sequence, e.observed_at))
        return events

    def for_request(self, execution_request_id: str) -> list[ExecutionRecord]:
        records = [
            self._load_current(entry)
            for entry in self.history()
            if entry.execution_request_id == execution_request_id
        ]
        return sorted(records, key=lambda record: record.created_at)

    def for_allocation(self, allocation_id: str) -> list[ExecutionRecord]:
        records = [
            self._load_current(entry)
            for entry in self.history()
            if entry.allocation_id == allocation_id
        ]
        return sorted(records, key=lambda record: record.created_at)

    def live_for_request(self, execution_request_id: str) -> list[ExecutionRecord]:
        """Attempts under this identity that may have reached the broker."""
        return [record for record in self.for_request(execution_request_id) if record.submitted]

    def history(self, limit: int | None = None) -> list[ExecutionHistoryEntry]:
        entries = [
            ExecutionHistoryEntry.from_json(payload)
            for payload in _read_lines(self.history_path, "execution history")
        ]
        entries.sort(key=lambda e: (e.created_at, e.execution_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    # --- runs --------------------------------------------------------------
    def save_run(self, result: ExecutionRunResult) -> str:
        if result.dry_run:
            raise ExecutionStoreError(
                f"refusing to store dry run {result.run_id}: it submitted nothing and must not "
                f"appear in the record of what was sent"
            )
        path = self.runs_dir / _filename(result.generated_at, result.run_id)
        payload = result.model_dump(mode="json")
        if path.exists():
            stored = _read_json(path)
            if stored != payload:
                raise ExecutionStoreError(
                    f"refusing to overwrite execution run {result.run_id}: a stored run is "
                    f"immutable and the existing record differs from the new one"
                )
            return str(path.relative_to(self._root))

        _write_json(path, payload)
        _append_line(self.run_history_path, ExecutionRunHistoryEntry.of(result).to_json())
        return str(path.relative_to(self._root))

    def get_run(self, run_id: str) -> ExecutionRunResult | None:
        for entry in self.run_history():
            if entry.run_id == run_id:
                return self._load_run(entry.generated_at, entry.run_id)
        return None

    def latest_run(self) -> ExecutionRunResult | None:
        entries = self.run_history()
        if not entries:
            return None
        return self._load_run(entries[0].generated_at, entries[0].run_id)

    def run_history(self, limit: int | None = None) -> list[ExecutionRunHistoryEntry]:
        entries = [
            ExecutionRunHistoryEntry.from_json(payload)
            for payload in _read_lines(self.run_history_path, "execution run history")
        ]
        entries.sort(key=lambda e: (e.generated_at, e.run_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    # --- internals ---------------------------------------------------------
    def _fold(self, record: ExecutionRecord) -> ExecutionRecord:
        """Apply every stored event in order.

        An event that would move the record along an illegal edge raises rather
        than being skipped: a history that cannot be replayed is a history that
        is wrong about something, and quietly ignoring the contradiction would
        leave the wrong state on screen.
        """
        current = record
        for event in self.events(record.execution_id):
            if event.state is current.state and event.sequence == 0:
                continue
            try:
                current = current.with_event(event)
            except ValueError as exc:
                raise ExecutionStoreError(
                    f"stored history for {record.execution_id} cannot be replayed at event "
                    f"{event.event_id}: {exc}"
                ) from exc
        return current

    def _load_current(self, entry: ExecutionHistoryEntry) -> ExecutionRecord:
        return self._fold(self._load(entry.created_at, entry.execution_id))

    def _load(self, created_at: datetime, execution_id: str) -> ExecutionRecord:
        path = self.executions_dir / _filename(created_at, execution_id)
        if not path.exists():
            raise ExecutionStoreError(f"execution record file is missing: {path}")
        try:
            return ExecutionRecord.model_validate(_read_json(path))
        except ExecutionStoreError:
            raise
        except Exception as exc:
            raise ExecutionStoreError(
                f"stored execution {execution_id} does not match the current model "
                f"(schema drift): {exc}"
            ) from exc

    def _load_run(self, generated_at: datetime, run_id: str) -> ExecutionRunResult:
        path = self.runs_dir / _filename(generated_at, run_id)
        if not path.exists():
            raise ExecutionStoreError(f"execution run file is missing: {path}")
        try:
            return ExecutionRunResult.model_validate(_read_json(path))
        except ExecutionStoreError:
            raise
        except Exception as exc:
            raise ExecutionStoreError(
                f"stored execution run {run_id} does not match the current model "
                f"(schema drift): {exc}"
            ) from exc


def has_live_attempt(records: list[ExecutionRecord]) -> ExecutionRecord | None:
    """The first attempt that may have reached the broker, if any.

    Deliberately includes ``SUBMISSION_PENDING`` and ``UNKNOWN`` via
    :attr:`ExecutionRecord.submitted`. Both mean *an order may be in flight*,
    and treating either as "nothing was sent" is precisely how one intention
    becomes two positions.
    """
    for record in records:
        if record.submitted:
            return record
    return None


def resolvable(record: ExecutionRecord) -> bool:
    """Whether this record is waiting on the broker rather than finished."""
    return record.state in (
        ExecutionState.SUBMITTED,
        ExecutionState.PARTIALLY_FILLED,
        ExecutionState.CANCEL_PENDING,
        ExecutionState.SUBMISSION_PENDING,
        ExecutionState.UNKNOWN,
    )


def _filename(stamp: datetime, identifier: str) -> str:
    return f"{stamp.astimezone(UTC).strftime(_COMPACT_TIME)}-{_SAFE.sub('_', identifier)}.json"


def _append_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        # Durability matters more here than anywhere else in the system: this
        # line is the evidence that an order may exist. Losing it to a buffer
        # is losing the reason not to send a second one.
        handle.flush()
        os.fsync(handle.fileno())


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
            raise ExecutionStoreError(f"corrupt {label} line {path}:{number}: {exc}") from exc
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
        raise ExecutionStoreError(f"corrupt stored execution {path}: {exc}") from exc
