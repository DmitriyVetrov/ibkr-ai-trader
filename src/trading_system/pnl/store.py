"""Persistence for realised profit and loss, daily roll-ups and settlements.

The same discipline as every other store in this system:

* **Immutable base records.** A result, a day and a settlement are written
  once. Writing different content under an existing id raises rather than
  overwriting — a realised result that changed silently would be a rewritten
  financial fact.
* **Append-only history.** ``history.jsonl``, ``daily.jsonl`` and
  ``settlements.jsonl`` gain a line per artifact and are never rewritten.
* **Atomic.** Written through a temporary file and ``os.replace``, because a
  half-written record is indistinguishable from a truncated one.
* **Content-addressed.** Ids derive from the fills and the amounts behind
  them, so recomputing an unchanged result records a *re-observation* rather
  than a second, differently-named copy of one fact. This is what makes the
  settlement job safe to run every fifteen minutes.

One thing is specific to this milestone. **There is no second capital ledger
here.** A settlement record explains what moved and why; the capital itself
moves in the Milestone 9 reservation ledger, as an appended event, folded on
read exactly as every other reservation event is. A store that also kept
balances would be a second copy of the truth, and when two copies disagree
there is no way to tell which is wrong.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trading_system.pnl.models import (
    DailyPnL,
    PnLRunResult,
    RealizedPnL,
    ReservationSettlement,
)

__all__ = [
    "FilesystemPnLRepository",
    "PnLHistoryEntry",
    "PnLRepository",
    "PnLStoreError",
    "SettlementHistoryEntry",
]

_COMPACT_TIME = "%Y%m%dT%H%M%S%fZ"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class PnLStoreError(RuntimeError):
    """A profit-and-loss artifact could not be stored or read back."""


@dataclass(frozen=True, slots=True)
class PnLHistoryEntry:
    """One line of the append-only result index.

    Carries the status and the figure so ``pnl history`` can be answered
    without opening every stored result — and so the question an operator
    actually asks of a long history, "which of these produced no number", is a
    scan rather than a load.
    """

    pnl_id: str
    position_id: str
    underlying: str
    strategy: str
    status: str
    realized_pnl: str | None
    currency: str | None
    session_date: str | None
    computed_at: datetime
    reobserved: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "pnl_id": self.pnl_id,
            "position_id": self.position_id,
            "underlying": self.underlying,
            "strategy": self.strategy,
            "status": self.status,
            "realized_pnl": self.realized_pnl,
            "currency": self.currency,
            "session_date": self.session_date,
            "computed_at": self.computed_at.astimezone(UTC).isoformat(),
            "reobserved": self.reobserved,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> PnLHistoryEntry:
        return cls(
            pnl_id=str(payload["pnl_id"]),
            position_id=str(payload["position_id"]),
            underlying=str(payload["underlying"]),
            strategy=str(payload["strategy"]),
            status=str(payload["status"]),
            realized_pnl=(
                None if payload.get("realized_pnl") is None else str(payload["realized_pnl"])
            ),
            currency=None if payload.get("currency") is None else str(payload["currency"]),
            session_date=(
                None if payload.get("session_date") is None else str(payload["session_date"])
            ),
            computed_at=datetime.fromisoformat(str(payload["computed_at"])),
            reobserved=bool(payload.get("reobserved", False)),
        )

    @classmethod
    def of(cls, record: RealizedPnL, *, reobserved: bool = False) -> PnLHistoryEntry:
        figure = record.best_available_pnl
        return cls(
            pnl_id=record.pnl_id,
            position_id=record.position_id,
            underlying=record.underlying,
            strategy=record.strategy.value,
            status=record.status.value,
            realized_pnl=None if figure is None else str(figure),
            currency=record.currency,
            session_date=None if record.session_date is None else record.session_date.isoformat(),
            computed_at=record.computed_at,
            reobserved=reobserved,
        )


@dataclass(frozen=True, slots=True)
class SettlementHistoryEntry:
    """One line of the append-only settlement index."""

    settlement_id: str
    reservation_id: str
    position_id: str
    status: str
    settled_amount: str
    currency: str
    block_reason: str | None
    settled_at: datetime
    reobserved: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "settlement_id": self.settlement_id,
            "reservation_id": self.reservation_id,
            "position_id": self.position_id,
            "status": self.status,
            "settled_amount": self.settled_amount,
            "currency": self.currency,
            "block_reason": self.block_reason,
            "settled_at": self.settled_at.astimezone(UTC).isoformat(),
            "reobserved": self.reobserved,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SettlementHistoryEntry:
        return cls(
            settlement_id=str(payload["settlement_id"]),
            reservation_id=str(payload["reservation_id"]),
            position_id=str(payload["position_id"]),
            status=str(payload["status"]),
            settled_amount=str(payload["settled_amount"]),
            currency=str(payload["currency"]),
            block_reason=(
                None if payload.get("block_reason") is None else str(payload["block_reason"])
            ),
            settled_at=datetime.fromisoformat(str(payload["settled_at"])),
            reobserved=bool(payload.get("reobserved", False)),
        )

    @classmethod
    def of(
        cls, settlement: ReservationSettlement, *, reobserved: bool = False
    ) -> SettlementHistoryEntry:
        return cls(
            settlement_id=settlement.settlement_id,
            reservation_id=settlement.reservation_id,
            position_id=settlement.position_id,
            status=settlement.status.value,
            settled_amount=str(settlement.settled_amount),
            currency=settlement.currency,
            block_reason=(
                None if settlement.block_reason is None else settlement.block_reason.value
            ),
            settled_at=settlement.settled_at,
            reobserved=reobserved,
        )


class PnLRepository(ABC):
    """What a profit-and-loss store has to be able to do."""

    @abstractmethod
    def save(self, record: RealizedPnL) -> tuple[str, bool]:
        """Store one realised result. Returns its id and whether it was new."""

    @abstractmethod
    def get(self, pnl_id: str) -> RealizedPnL | None: ...

    @abstractmethod
    def for_position(self, position_id: str) -> list[RealizedPnL]:
        """Every result recorded for one position, oldest first."""

    @abstractmethod
    def all(self, limit: int | None = None) -> list[RealizedPnL]: ...

    @abstractmethod
    def history(self, limit: int | None = None) -> list[PnLHistoryEntry]: ...

    @abstractmethod
    def save_daily(self, record: DailyPnL) -> tuple[str, bool]: ...

    @abstractmethod
    def daily(self, session_date: date) -> DailyPnL | None:
        """The most recent roll-up recorded for one exchange-local day."""

    @abstractmethod
    def daily_history(self, limit: int | None = None) -> list[DailyPnL]: ...

    @abstractmethod
    def save_settlement(self, settlement: ReservationSettlement) -> tuple[str, bool]: ...

    @abstractmethod
    def settlement(self, settlement_id: str) -> ReservationSettlement | None: ...

    @abstractmethod
    def settlements_for(self, reservation_id: str) -> list[ReservationSettlement]:
        """Every settlement attempt against one reservation, oldest first."""

    @abstractmethod
    def settlement_history(self, limit: int | None = None) -> list[SettlementHistoryEntry]: ...

    @abstractmethod
    def save_run(self, result: PnLRunResult) -> str: ...

    @abstractmethod
    def latest_run(self) -> PnLRunResult | None: ...


class FilesystemPnLRepository(PnLRepository):
    """JSON-on-disk store, rooted at ``<data>/pnl/``.

    ::

        pnl/
          results/<session>/<pnl_id>.json
          daily/<session_date>/<daily_pnl_id>.json
          settlements/<day>/<settlement_id>.json
          runs/<day>/<run_id>.json
          history.jsonl
          daily.jsonl
          settlements.jsonl
          runs.jsonl
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    # --- layout ------------------------------------------------------------
    @property
    def root(self) -> Path:
        return self._root

    @property
    def results_dir(self) -> Path:
        return self._root / "results"

    @property
    def daily_dir(self) -> Path:
        return self._root / "daily"

    @property
    def settlements_dir(self) -> Path:
        return self._root / "settlements"

    @property
    def runs_dir(self) -> Path:
        return self._root / "runs"

    @property
    def history_path(self) -> Path:
        return self._root / "history.jsonl"

    @property
    def daily_history_path(self) -> Path:
        return self._root / "daily.jsonl"

    @property
    def settlement_history_path(self) -> Path:
        return self._root / "settlements.jsonl"

    @property
    def runs_history_path(self) -> Path:
        return self._root / "runs.jsonl"

    # --- realised results --------------------------------------------------
    def save(self, record: RealizedPnL) -> tuple[str, bool]:
        path = self._result_path(record)
        payload = record.model_dump(mode="json")
        is_new = _write_once(path, payload, what=f"realised profit and loss {record.pnl_id}")
        _append_line(self.history_path, PnLHistoryEntry.of(record, reobserved=not is_new).to_json())
        return record.pnl_id, is_new

    def get(self, pnl_id: str) -> RealizedPnL | None:
        for entry in self.history():
            if entry.pnl_id == pnl_id:
                return self._load_result(entry)
        return None

    def for_position(self, position_id: str) -> list[RealizedPnL]:
        seen: set[str] = set()
        records: list[RealizedPnL] = []
        for entry in sorted(self.history(), key=lambda e: (e.computed_at, e.pnl_id)):
            if entry.position_id != position_id or entry.pnl_id in seen:
                continue
            seen.add(entry.pnl_id)
            record = self._load_result(entry)
            if record is not None:
                records.append(record)
        return records

    def all(self, limit: int | None = None) -> list[RealizedPnL]:
        seen: set[str] = set()
        records: list[RealizedPnL] = []
        for entry in self.history():
            if entry.pnl_id in seen:
                continue
            seen.add(entry.pnl_id)
            record = self._load_result(entry)
            if record is not None:
                records.append(record)
            if limit is not None and len(records) >= limit:
                break
        return records

    def history(self, limit: int | None = None) -> list[PnLHistoryEntry]:
        entries = [
            PnLHistoryEntry.from_json(payload)
            for payload in _read_lines(self.history_path, "profit-and-loss history")
        ]
        entries.sort(key=lambda e: (e.computed_at, e.pnl_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    # --- daily -------------------------------------------------------------
    def save_daily(self, record: DailyPnL) -> tuple[str, bool]:
        path = (
            self.daily_dir / record.session_date.isoformat() / f"{_safe(record.daily_pnl_id)}.json"
        )
        payload = record.model_dump(mode="json")
        is_new = _write_once(path, payload, what=f"daily profit and loss {record.daily_pnl_id}")
        _append_line(
            self.daily_history_path,
            {
                "daily_pnl_id": record.daily_pnl_id,
                "session_date": record.session_date.isoformat(),
                "status": record.status.value,
                "realized_pnl": (None if record.realized_pnl is None else str(record.realized_pnl)),
                "currency": record.currency,
                "positions_closed": record.positions_closed,
                "computed_at": record.computed_at.astimezone(UTC).isoformat(),
                "reobserved": not is_new,
            },
        )
        return record.daily_pnl_id, is_new

    def daily(self, session_date: date) -> DailyPnL | None:
        candidates = [
            payload
            for payload in _read_lines(self.daily_history_path, "daily profit and loss history")
            if str(payload.get("session_date")) == session_date.isoformat()
        ]
        if not candidates:
            return None
        newest = max(candidates, key=lambda payload: str(payload.get("computed_at", "")))
        return self._load_daily(session_date, str(newest["daily_pnl_id"]))

    def daily_history(self, limit: int | None = None) -> list[DailyPnL]:
        seen: set[str] = set()
        records: list[DailyPnL] = []
        payloads = _read_lines(self.daily_history_path, "daily profit and loss history")
        payloads.sort(key=lambda payload: str(payload.get("computed_at", "")), reverse=True)
        for payload in payloads:
            identifier = str(payload["daily_pnl_id"])
            if identifier in seen:
                continue
            seen.add(identifier)
            record = self._load_daily(date.fromisoformat(str(payload["session_date"])), identifier)
            if record is not None:
                records.append(record)
            if limit is not None and len(records) >= limit:
                break
        return records

    # --- settlements -------------------------------------------------------
    def save_settlement(self, settlement: ReservationSettlement) -> tuple[str, bool]:
        path = self._settlement_path(settlement)
        payload = settlement.model_dump(mode="json")
        is_new = _write_once(path, payload, what=f"settlement {settlement.settlement_id}")
        _append_line(
            self.settlement_history_path,
            SettlementHistoryEntry.of(settlement, reobserved=not is_new).to_json(),
        )
        return settlement.settlement_id, is_new

    def settlement(self, settlement_id: str) -> ReservationSettlement | None:
        for entry in self.settlement_history():
            if entry.settlement_id == settlement_id:
                return self._load_settlement(entry)
        return None

    def settlements_for(self, reservation_id: str) -> list[ReservationSettlement]:
        seen: set[str] = set()
        records: list[ReservationSettlement] = []
        for entry in sorted(
            self.settlement_history(), key=lambda e: (e.settled_at, e.settlement_id)
        ):
            if entry.reservation_id != reservation_id or entry.settlement_id in seen:
                continue
            seen.add(entry.settlement_id)
            record = self._load_settlement(entry)
            if record is not None:
                records.append(record)
        return records

    def settlement_history(self, limit: int | None = None) -> list[SettlementHistoryEntry]:
        entries = [
            SettlementHistoryEntry.from_json(payload)
            for payload in _read_lines(self.settlement_history_path, "settlement history")
        ]
        entries.sort(key=lambda e: (e.settled_at, e.settlement_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    # --- runs --------------------------------------------------------------
    def save_run(self, result: PnLRunResult) -> str:
        path = (
            self.runs_dir
            / result.as_of.astimezone(UTC).strftime("%Y-%m-%d")
            / f"{_safe(result.run_id)}.json"
        )
        _write_once(
            path,
            result.model_dump(mode="json"),
            what=f"profit-and-loss run {result.run_id}",
        )
        _append_line(
            self.runs_history_path,
            {
                "run_id": result.run_id,
                "as_of": result.as_of.astimezone(UTC).isoformat(),
                "generated_at": result.generated_at.astimezone(UTC).isoformat(),
                "dry_run": result.dry_run,
                "results_computed": result.results_computed,
                "settlements_applied": result.settlements_applied,
                "capital_returned": str(result.capital_returned),
            },
        )
        return result.run_id

    def latest_run(self) -> PnLRunResult | None:
        payloads = _read_lines(self.runs_history_path, "profit-and-loss run history")
        if not payloads:
            return None
        newest = max(payloads, key=lambda payload: str(payload.get("generated_at", "")))
        as_of = datetime.fromisoformat(str(newest["as_of"]))
        path = (
            self.runs_dir
            / as_of.astimezone(UTC).strftime("%Y-%m-%d")
            / f"{_safe(str(newest['run_id']))}.json"
        )
        if not path.is_file():
            return None
        return PnLRunResult.model_validate(json.loads(path.read_text(encoding="utf-8")))

    # --- internals ---------------------------------------------------------
    def _result_path(self, record: RealizedPnL) -> Path:
        bucket = (
            record.session_date.isoformat()
            if record.session_date is not None
            else record.computed_at.astimezone(UTC).strftime("%Y-%m-%d")
        )
        return self.results_dir / bucket / f"{_safe(record.pnl_id)}.json"

    def _settlement_path(self, settlement: ReservationSettlement) -> Path:
        bucket = settlement.settled_at.astimezone(UTC).strftime("%Y-%m-%d")
        return self.settlements_dir / bucket / f"{_safe(settlement.settlement_id)}.json"

    def _load_result(self, entry: PnLHistoryEntry) -> RealizedPnL | None:
        buckets = [entry.session_date] if entry.session_date else []
        buckets.append(entry.computed_at.astimezone(UTC).strftime("%Y-%m-%d"))
        for bucket in buckets:
            path = self.results_dir / bucket / f"{_safe(entry.pnl_id)}.json"
            if path.is_file():
                return RealizedPnL.model_validate(json.loads(path.read_text(encoding="utf-8")))
        return None

    def _load_daily(self, session_date: date, daily_pnl_id: str) -> DailyPnL | None:
        path = self.daily_dir / session_date.isoformat() / f"{_safe(daily_pnl_id)}.json"
        if not path.is_file():
            return None
        return DailyPnL.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def _load_settlement(self, entry: SettlementHistoryEntry) -> ReservationSettlement | None:
        bucket = entry.settled_at.astimezone(UTC).strftime("%Y-%m-%d")
        path = self.settlements_dir / bucket / f"{_safe(entry.settlement_id)}.json"
        if not path.is_file():
            return None
        return ReservationSettlement.model_validate(json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Filesystem primitives
# ---------------------------------------------------------------------------
def _safe(name: str) -> str:
    return _SAFE.sub("_", name)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Write through a temporary file, then rename. Never a partial record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:  # pragma: no cover - defensive
        Path(temporary).unlink(missing_ok=True)
        raise


def _write_once(path: Path, payload: dict[str, Any], *, what: str) -> bool:
    """Write a record that must never change. Returns whether it was new.

    Identical content under an existing id is a *re-observation* and is
    accepted silently; different content is a contradiction and raises. A
    realised result that changed under the same id would be a rewritten
    financial fact, which is exactly the thing an audit trail exists to make
    impossible.
    """
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing == payload:
            return False
        raise PnLStoreError(
            f"{what} already exists with different content at {path}. Stored financial "
            f"results are immutable: a figure that changed under the same id could not be "
            f"audited, because there would be no record of what it used to say."
        )
    _atomic_write(path, payload)
    return True


def _append_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")


def _read_lines(path: Path, what: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    payloads: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payloads.append(json.loads(stripped))
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise PnLStoreError(f"{what} line {number} in {path} is not valid JSON") from exc
    return payloads
