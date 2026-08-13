"""Persistence for the reservation ledger.

The same discipline as the execution store, and for the same reason: this is a
record of where money went. If it disagrees with reality the campaign will
either refuse a trade it could afford or authorise one it cannot.

* **Immutable base record.** A reservation's file is written once. A later
  consumption does not edit it — it appends an event, and the current state is
  *folded* from the events. That is why a reservation that consumed 605 can
  still show that it once held 1,210 in reserve.
* **Append-only history.** ``history.jsonl`` gains a line per reservation and
  ``events/<reservation_id>.jsonl`` a line per observation. Neither is ever
  rewritten.
* **Atomic.** Written through a temporary file and ``os.replace``.

A history that cannot be replayed raises rather than being skipped. A
contradiction in a money ledger is worth surfacing loudly; quietly ignoring one
leaves the wrong balance on screen and no trace of why.
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

from trading_system.reservations.models import Reservation, ReservationEvent

__all__ = [
    "FilesystemReservationRepository",
    "ReservationHistoryEntry",
    "ReservationRepository",
    "ReservationStoreError",
]

_COMPACT_TIME = "%Y%m%dT%H%M%S%fZ"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class ReservationStoreError(RuntimeError):
    """A reservation could not be stored or read back."""


@dataclass(frozen=True, slots=True)
class ReservationHistoryEntry:
    """One line of the append-only reservation index."""

    reservation_id: str
    campaign_id: str
    allocation_id: str
    opportunity_id: str
    symbol: str
    created_at: datetime
    authorized_amount: str
    currency: str

    def to_json(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "campaign_id": self.campaign_id,
            "allocation_id": self.allocation_id,
            "opportunity_id": self.opportunity_id,
            "symbol": self.symbol,
            "created_at": self.created_at.astimezone(UTC).isoformat(),
            "authorized_amount": self.authorized_amount,
            "currency": self.currency,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ReservationHistoryEntry:
        return cls(
            reservation_id=str(payload["reservation_id"]),
            campaign_id=str(payload["campaign_id"]),
            allocation_id=str(payload["allocation_id"]),
            opportunity_id=str(payload["opportunity_id"]),
            symbol=str(payload["symbol"]),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            authorized_amount=str(payload["authorized_amount"]),
            currency=str(payload["currency"]),
        )

    @classmethod
    def of(cls, reservation: Reservation) -> ReservationHistoryEntry:
        return cls(
            reservation_id=reservation.reservation_id,
            campaign_id=reservation.campaign_id,
            allocation_id=reservation.allocation_id,
            opportunity_id=reservation.opportunity_id,
            symbol=reservation.symbol,
            created_at=reservation.created_at,
            authorized_amount=str(reservation.authorized_amount),
            currency=reservation.currency,
        )


class ReservationRepository(ABC):
    """Storage interface for reservations and their economic history."""

    @abstractmethod
    def save(self, reservation: Reservation) -> str:
        """Persist a reservation's base record immutably. Returns its storage id."""

    @abstractmethod
    def append_event(self, event: ReservationEvent) -> bool:
        """Append one observation. Returns whether it was new.

        A replayed event is not new information and records nothing — which is
        what stops a second reconciliation over unchanged broker state from
        consuming or releasing the same capital twice.
        """

    @abstractmethod
    def base(self, reservation_id: str) -> Reservation | None:
        """The record exactly as first written, before anything moved."""

    @abstractmethod
    def current(self, reservation_id: str) -> Reservation | None:
        """The record with every stored event folded in."""

    @abstractmethod
    def events(self, reservation_id: str) -> list[ReservationEvent]: ...

    @abstractmethod
    def history(self, limit: int | None = None) -> list[ReservationHistoryEntry]: ...

    @abstractmethod
    def all_current(self) -> list[Reservation]:
        """Every reservation, folded. The campaign's committed capital, in full."""

    @abstractmethod
    def for_allocation(self, allocation_id: str) -> Reservation | None: ...

    @abstractmethod
    def for_campaign(self, campaign_id: str) -> list[Reservation]: ...


class FilesystemReservationRepository(ReservationRepository):
    """JSON-on-disk store, rooted at ``<data>/reservations/``."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def reservations_dir(self) -> Path:
        return self._root / "reservations"

    @property
    def events_dir(self) -> Path:
        return self._root / "events"

    @property
    def history_path(self) -> Path:
        return self._root / "history.jsonl"

    def event_path(self, reservation_id: str) -> Path:
        return self.events_dir / f"{_SAFE.sub('_', reservation_id)}.jsonl"

    def save(self, reservation: Reservation) -> str:
        path = self.reservations_dir / _filename(reservation.created_at, reservation.reservation_id)
        payload = reservation.model_dump(mode="json")
        if path.exists():
            stored = _read_json(path)
            if stored != payload:
                raise ReservationStoreError(
                    f"refusing to overwrite reservation {reservation.reservation_id}: a stored "
                    f"reservation is immutable and the existing record differs from the new "
                    f"one. Later movements are appended as events, never written over the "
                    f"original authorisation"
                )
            return str(path.relative_to(self._root))

        _write_json(path, payload)
        _append_line(self.history_path, ReservationHistoryEntry.of(reservation).to_json())
        return str(path.relative_to(self._root))

    def append_event(self, event: ReservationEvent) -> bool:
        existing = {stored.event_id for stored in self.events(event.reservation_id)}
        if event.event_id in existing:
            return False
        _append_line(self.event_path(event.reservation_id), event.model_dump(mode="json"))
        return True

    def base(self, reservation_id: str) -> Reservation | None:
        for entry in self.history():
            if entry.reservation_id == reservation_id:
                return self._load(entry.created_at, entry.reservation_id)
        return None

    def current(self, reservation_id: str) -> Reservation | None:
        record = self.base(reservation_id)
        if record is None:
            return None
        return self._fold(record)

    def events(self, reservation_id: str) -> list[ReservationEvent]:
        payloads = _read_lines(self.event_path(reservation_id), f"{reservation_id} events")
        events = [ReservationEvent.model_validate(payload) for payload in payloads]
        events.sort(key=lambda event: (event.sequence, event.observed_at))
        return events

    def history(self, limit: int | None = None) -> list[ReservationHistoryEntry]:
        entries = [
            ReservationHistoryEntry.from_json(payload)
            for payload in _read_lines(self.history_path, "reservation history")
        ]
        entries.sort(key=lambda e: (e.created_at, e.reservation_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def all_current(self) -> list[Reservation]:
        reservations = [
            self._fold(self._load(entry.created_at, entry.reservation_id))
            for entry in self.history()
        ]
        return sorted(reservations, key=lambda r: (r.created_at, r.reservation_id))

    def for_allocation(self, allocation_id: str) -> Reservation | None:
        for entry in self.history():
            if entry.allocation_id == allocation_id:
                return self.current(entry.reservation_id)
        return None

    def for_campaign(self, campaign_id: str) -> list[Reservation]:
        return [
            reservation
            for reservation in self.all_current()
            if reservation.campaign_id == campaign_id
        ]

    def _fold(self, reservation: Reservation) -> Reservation:
        current = reservation
        for event in self.events(reservation.reservation_id):
            try:
                current = current.with_event(event)
            except ValueError as exc:
                raise ReservationStoreError(
                    f"stored history for {reservation.reservation_id} cannot be replayed at "
                    f"event {event.event_id}: {exc}"
                ) from exc
        return current

    def _load(self, created_at: datetime, reservation_id: str) -> Reservation:
        path = self.reservations_dir / _filename(created_at, reservation_id)
        if not path.exists():
            raise ReservationStoreError(f"reservation file is missing: {path}")
        try:
            return Reservation.model_validate(_read_json(path))
        except ReservationStoreError:
            raise
        except Exception as exc:
            raise ReservationStoreError(
                f"stored reservation {reservation_id} does not match the current model "
                f"(schema drift): {exc}"
            ) from exc


def _filename(stamp: datetime, identifier: str) -> str:
    return f"{stamp.astimezone(UTC).strftime(_COMPACT_TIME)}-{_SAFE.sub('_', identifier)}.json"


def _append_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        # Durability matters here for the same reason it does in the execution
        # ledger: this line is the evidence that capital moved.
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
            raise ReservationStoreError(f"corrupt {label} line {path}:{number}: {exc}") from exc
    return payloads


def _write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically. A half-written reservation would still be read back."""
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
        raise ReservationStoreError(f"corrupt stored reservation {path}: {exc}") from exc
