"""Persistence for allocation runs.

The same discipline as the universe, research and strategy stores, and here it
carries more weight than anywhere else: this ledger *is* the campaign's
accounting. Rewriting one line of it would change what the campaign believes it
has committed, and therefore what it believes it may commit next.

* **Immutable.** A run file is written once. Writing different content to an
  existing id raises rather than overwriting. A later account balance never
  edits an earlier authorisation — a new run makes a new decision.
* **Append-only indexes.** ``history.jsonl`` gains a line per run and
  ``symbols/<SYMBOL>.jsonl`` a line per allocation. Neither is ever rewritten,
  so a later refusal cannot erase an earlier authorisation.
* **Atomic.** Written through a temporary file and ``os.replace``, because a
  half-written run is indistinguishable from a truncated one and both would be
  read back as campaign state.

Dry runs are never stored here. A diagnostic result that entered the ledger
would consume budget nobody committed, which is precisely what ``--dry-run``
promises not to do.

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
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_system.allocation.models import AllocationRunResult, CampaignAllocation

__all__ = [
    "AllocationHistoryEntry",
    "AllocationRepository",
    "AllocationStoreError",
    "FilesystemAllocationRepository",
    "SymbolAllocationEntry",
]

_COMPACT_TIME = "%Y%m%dT%H%M%S%fZ"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class AllocationStoreError(RuntimeError):
    """An allocation run could not be stored or read back."""


@dataclass(frozen=True, slots=True)
class AllocationHistoryEntry:
    """One line of the append-only allocation-run history."""

    run_id: str
    campaign_id: str
    as_of: datetime
    generated_at: datetime
    status: str
    symbols: tuple[str, ...]
    approved: int
    rejected: int
    no_trade: int
    allocated: str
    available_after: str
    contract_run_id: str | None = None
    policy_version: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "campaign_id": self.campaign_id,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "generated_at": self.generated_at.astimezone(UTC).isoformat(),
            "status": self.status,
            "symbols": list(self.symbols),
            "approved": self.approved,
            "rejected": self.rejected,
            "no_trade": self.no_trade,
            "allocated": self.allocated,
            "available_after": self.available_after,
            "contract_run_id": self.contract_run_id,
            "policy_version": self.policy_version,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> AllocationHistoryEntry:
        return cls(
            run_id=str(payload["run_id"]),
            campaign_id=str(payload["campaign_id"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            generated_at=datetime.fromisoformat(str(payload["generated_at"])),
            status=str(payload["status"]),
            symbols=tuple(str(s) for s in payload.get("symbols", [])),
            approved=int(payload.get("approved", 0)),
            rejected=int(payload.get("rejected", 0)),
            no_trade=int(payload.get("no_trade", 0)),
            allocated=str(payload.get("allocated", "0")),
            available_after=str(payload.get("available_after", "0")),
            contract_run_id=payload.get("contract_run_id"),
            policy_version=payload.get("policy_version"),
        )

    @classmethod
    def of(cls, result: AllocationRunResult) -> AllocationHistoryEntry:
        return cls(
            run_id=result.run_id,
            campaign_id=result.campaign_id,
            as_of=result.as_of,
            generated_at=result.generated_at,
            status=result.status.value,
            symbols=tuple(result.symbols),
            approved=result.counts.approved,
            rejected=result.counts.rejected,
            no_trade=result.counts.no_trade,
            allocated=str(result.allocated_this_run),
            available_after=str(result.available_after),
            contract_run_id=result.contract_run_id,
            policy_version=result.policy_version,
        )


@dataclass(frozen=True, slots=True)
class SymbolAllocationEntry:
    """One line of a symbol's append-only allocation history."""

    symbol: str
    run_id: str
    allocation_id: str
    opportunity_id: str
    as_of: datetime
    decided_at: datetime
    outcome: str
    strategy: str
    quantity: int = 0
    capital_committed: str = "0"
    max_loss: str = "0"
    reason_codes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "run_id": self.run_id,
            "allocation_id": self.allocation_id,
            "opportunity_id": self.opportunity_id,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "decided_at": self.decided_at.astimezone(UTC).isoformat(),
            "outcome": self.outcome,
            "strategy": self.strategy,
            "quantity": self.quantity,
            "capital_committed": self.capital_committed,
            "max_loss": self.max_loss,
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SymbolAllocationEntry:
        return cls(
            symbol=str(payload["symbol"]),
            run_id=str(payload["run_id"]),
            allocation_id=str(payload["allocation_id"]),
            opportunity_id=str(payload["opportunity_id"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            decided_at=datetime.fromisoformat(str(payload["decided_at"])),
            outcome=str(payload["outcome"]),
            strategy=str(payload["strategy"]),
            quantity=int(payload.get("quantity", 0)),
            capital_committed=str(payload.get("capital_committed", "0")),
            max_loss=str(payload.get("max_loss", "0")),
            reason_codes=tuple(str(c) for c in payload.get("reason_codes", [])),
        )

    @classmethod
    def of(cls, allocation: CampaignAllocation) -> SymbolAllocationEntry:
        return cls(
            symbol=allocation.symbol,
            run_id=allocation.run_id,
            allocation_id=allocation.allocation_id,
            opportunity_id=allocation.opportunity_id,
            as_of=allocation.as_of,
            decided_at=allocation.decided_at,
            outcome=allocation.outcome.value,
            strategy=allocation.strategy.value,
            quantity=allocation.quantity,
            capital_committed=str(allocation.capital_committed),
            max_loss=str(allocation.total_max_loss),
            reason_codes=tuple(code.value for code in allocation.reason_codes),
        )


class AllocationRepository(ABC):
    """Storage interface for allocation runs."""

    @abstractmethod
    def save(self, result: AllocationRunResult) -> str:
        """Persist a run immutably. Returns its storage id."""

    @abstractmethod
    def get(self, run_id: str) -> AllocationRunResult | None: ...

    @abstractmethod
    def latest(self) -> AllocationRunResult | None: ...

    @abstractmethod
    def all_runs(self) -> list[AllocationRunResult]:
        """Every stored run, oldest first.

        Campaign state is replayed from these rather than kept as a running
        total: a running balance is a second copy of the truth, and when it
        drifts there is no way to tell which copy is wrong.
        """

    @abstractmethod
    def history(self, limit: int | None = None) -> list[AllocationHistoryEntry]: ...

    @abstractmethod
    def symbol_history(self, symbol: str, limit: int | None = None) -> list[SymbolAllocationEntry]:
        """One underlying's allocation history, newest first."""


class FilesystemAllocationRepository(AllocationRepository):
    """JSON-on-disk store, rooted at ``<data>/allocation/``."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def runs_dir(self) -> Path:
        return self._root / "runs"

    @property
    def history_path(self) -> Path:
        return self._root / "history.jsonl"

    def symbol_path(self, symbol: str) -> Path:
        return self._root / "symbols" / f"{_SAFE.sub('_', symbol.strip().upper())}.jsonl"

    def save(self, result: AllocationRunResult) -> str:
        if result.dry_run:
            raise AllocationStoreError(
                f"refusing to store dry run {result.run_id}: a diagnostic result that entered "
                f"the ledger would consume budget nobody committed"
            )
        path = self.runs_dir / _run_filename(result.generated_at, result.run_id)
        payload = result.model_dump(mode="json")
        if path.exists():
            stored = _read_json(path)
            if stored != payload:
                raise AllocationStoreError(
                    f"refusing to overwrite allocation run {result.run_id}: a stored run is "
                    f"immutable and the existing record differs from the new one"
                )
            return str(path.relative_to(self._root))

        _write_json(path, payload)
        _append_line(self.history_path, AllocationHistoryEntry.of(result).to_json())
        for allocation in result.allocations:
            _append_line(
                self.symbol_path(allocation.symbol),
                SymbolAllocationEntry.of(allocation).to_json(),
            )
        return str(path.relative_to(self._root))

    def get(self, run_id: str) -> AllocationRunResult | None:
        for entry in self.history():
            if entry.run_id == run_id:
                return self._load(entry.generated_at, entry.run_id)
        return None

    def latest(self) -> AllocationRunResult | None:
        entries = self.history()
        if not entries:
            return None
        return self._load(entries[0].generated_at, entries[0].run_id)

    def all_runs(self) -> list[AllocationRunResult]:
        entries = sorted(self.history(), key=lambda e: (e.generated_at, e.run_id))
        return [self._load(entry.generated_at, entry.run_id) for entry in entries]

    def history(self, limit: int | None = None) -> list[AllocationHistoryEntry]:
        entries = [
            AllocationHistoryEntry.from_json(payload)
            for payload in _read_lines(self.history_path, "allocation history")
        ]
        entries.sort(key=lambda e: (e.generated_at, e.run_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def symbol_history(self, symbol: str, limit: int | None = None) -> list[SymbolAllocationEntry]:
        entries = [
            SymbolAllocationEntry.from_json(payload)
            for payload in _read_lines(self.symbol_path(symbol), f"{symbol} allocation history")
        ]
        entries.sort(key=lambda e: (e.decided_at, e.run_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def committed_total(self) -> Decimal:
        """Every euro this ledger has authorised. Replayed, never accumulated."""
        return sum(
            (
                allocation.capital_committed
                for run in self.all_runs()
                for allocation in run.allocations
                if allocation.approved
            ),
            Decimal("0"),
        )

    def _load(self, generated_at: datetime, run_id: str) -> AllocationRunResult:
        path = self.runs_dir / _run_filename(generated_at, run_id)
        if not path.exists():
            raise AllocationStoreError(f"allocation run file is missing: {path}")
        try:
            return AllocationRunResult.model_validate(_read_json(path))
        except AllocationStoreError:
            raise
        except Exception as exc:
            raise AllocationStoreError(
                f"stored allocation run {run_id} does not match the current model "
                f"(schema drift): {exc}"
            ) from exc


def _run_filename(generated_at: datetime, run_id: str) -> str:
    return f"{generated_at.astimezone(UTC).strftime(_COMPACT_TIME)}-{_SAFE.sub('_', run_id)}.json"


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
            raise AllocationStoreError(f"corrupt {label} line {path}:{number}: {exc}") from exc
    return payloads


def _write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically. A half-written run would still be read as history."""
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
        raise AllocationStoreError(f"corrupt stored run {path}: {exc}") from exc
