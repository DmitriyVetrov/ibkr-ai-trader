"""Persistence for strategy decisions and contract selections.

Every run is stored, and no run replaces another. The value of the history is
answering *later* — when the configuration, the chain and the model have all
moved on — what the system decided on 12 August, which strategy it chose, which
contract that resolved to, and what it rejected on the way. A store that kept
only the latest decision would make every past trade unexplainable and every
evaluation circular.

The discipline mirrors the research and universe stores, for the same reasons:

* **Immutable.** A run file is written once. Writing different content to an
  existing id raises rather than overwriting.
* **Append-only indexes.** ``history.jsonl`` gains a line per run and
  ``symbols/<SYMBOL>.jsonl`` a line per decision or selection. Neither is ever
  rewritten, so a later failed run cannot erase an earlier successful one.
* **Atomic.** Written through a temporary file and ``os.replace``, because a
  half-written run is indistinguishable from a truncated one and both would be
  read back as history.

The abstract repositories are what consumers see; the filesystem is one
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

from trading_system.strategies.models import (
    ContractSelectionResult,
    ContractSelectionRunResult,
    StrategyDecisionRecord,
    StrategyRunResult,
)

__all__ = [
    "ContractHistoryEntry",
    "ContractSelectionRepository",
    "FilesystemContractSelectionRepository",
    "FilesystemStrategyRepository",
    "StrategyHistoryEntry",
    "StrategyRepository",
    "StrategyStoreError",
    "SymbolDecisionEntry",
    "SymbolSelectionEntry",
]

_COMPACT_TIME = "%Y%m%dT%H%M%S%fZ"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class StrategyStoreError(RuntimeError):
    """A strategy or contract run could not be stored or read back."""


# ---------------------------------------------------------------------------
# Index entries
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StrategyHistoryEntry:
    """One line of the append-only strategy-run history."""

    run_id: str
    as_of: datetime
    generated_at: datetime
    status: str
    symbols: tuple[str, ...]
    proposed: int
    no_trade: int
    failed: int
    research_run_id: str | None = None
    model_name: str | None = None
    prompt_version: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "generated_at": self.generated_at.astimezone(UTC).isoformat(),
            "status": self.status,
            "symbols": list(self.symbols),
            "proposed": self.proposed,
            "no_trade": self.no_trade,
            "failed": self.failed,
            "research_run_id": self.research_run_id,
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> StrategyHistoryEntry:
        return cls(
            run_id=str(payload["run_id"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            generated_at=datetime.fromisoformat(str(payload["generated_at"])),
            status=str(payload["status"]),
            symbols=tuple(str(s) for s in payload.get("symbols", [])),
            proposed=int(payload.get("proposed", 0)),
            no_trade=int(payload.get("no_trade", 0)),
            failed=int(payload.get("failed", 0)),
            research_run_id=payload.get("research_run_id"),
            model_name=payload.get("model_name"),
            prompt_version=payload.get("prompt_version"),
        )

    @classmethod
    def of(cls, result: StrategyRunResult) -> StrategyHistoryEntry:
        model = next(
            (d.agent_metadata for d in result.decisions if d.agent_metadata is not None), None
        )
        return cls(
            run_id=result.run_id,
            as_of=result.as_of,
            generated_at=result.generated_at,
            status=result.status.value,
            symbols=tuple(result.symbols),
            proposed=result.counts.proposed,
            no_trade=result.counts.no_trade,
            failed=result.counts.failed,
            research_run_id=result.research_run_id,
            model_name=model.model_name if model else None,
            prompt_version=model.prompt_version if model else None,
        )


@dataclass(frozen=True, slots=True)
class SymbolDecisionEntry:
    """One line of a symbol's append-only decision history."""

    symbol: str
    run_id: str
    decision_id: str
    as_of: datetime
    generated_at: datetime
    status: str
    action: str
    strategy: str | None = None
    hypothesis: str | None = None
    confidence: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "run_id": self.run_id,
            "decision_id": self.decision_id,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "generated_at": self.generated_at.astimezone(UTC).isoformat(),
            "status": self.status,
            "action": self.action,
            "strategy": self.strategy,
            "hypothesis": self.hypothesis,
            "confidence": self.confidence,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SymbolDecisionEntry:
        return cls(
            symbol=str(payload["symbol"]),
            run_id=str(payload["run_id"]),
            decision_id=str(payload["decision_id"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            generated_at=datetime.fromisoformat(str(payload["generated_at"])),
            status=str(payload["status"]),
            action=str(payload["action"]),
            strategy=payload.get("strategy"),
            hypothesis=payload.get("hypothesis"),
            confidence=payload.get("confidence"),
        )

    @classmethod
    def of(cls, decision: StrategyDecisionRecord) -> SymbolDecisionEntry:
        return cls(
            symbol=decision.symbol,
            run_id=decision.run_id,
            decision_id=decision.decision_id,
            as_of=decision.as_of,
            generated_at=decision.generated_at,
            status=decision.status.value,
            action=decision.action.value,
            strategy=decision.selected_strategy.value if decision.selected_strategy else None,
            hypothesis=decision.hypothesis.value if decision.hypothesis else None,
            confidence=decision.confidence.value if decision.confidence else None,
        )


@dataclass(frozen=True, slots=True)
class ContractHistoryEntry:
    """One line of the append-only contract-selection history."""

    run_id: str
    as_of: datetime
    generated_at: datetime
    status: str
    symbols: tuple[str, ...]
    selected: int
    no_contract: int
    strategy_run_id: str | None = None
    selection_policy_version: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "generated_at": self.generated_at.astimezone(UTC).isoformat(),
            "status": self.status,
            "symbols": list(self.symbols),
            "selected": self.selected,
            "no_contract": self.no_contract,
            "strategy_run_id": self.strategy_run_id,
            "selection_policy_version": self.selection_policy_version,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ContractHistoryEntry:
        return cls(
            run_id=str(payload["run_id"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            generated_at=datetime.fromisoformat(str(payload["generated_at"])),
            status=str(payload["status"]),
            symbols=tuple(str(s) for s in payload.get("symbols", [])),
            selected=int(payload.get("selected", 0)),
            no_contract=int(payload.get("no_contract", 0)),
            strategy_run_id=payload.get("strategy_run_id"),
            selection_policy_version=payload.get("selection_policy_version"),
        )

    @classmethod
    def of(cls, result: ContractSelectionRunResult) -> ContractHistoryEntry:
        return cls(
            run_id=result.run_id,
            as_of=result.as_of,
            generated_at=result.generated_at,
            status=result.status.value,
            symbols=tuple(result.symbols),
            selected=result.counts.selected,
            no_contract=result.counts.no_contract,
            strategy_run_id=result.strategy_run_id,
            selection_policy_version=result.selection_policy_version,
        )


@dataclass(frozen=True, slots=True)
class SymbolSelectionEntry:
    """One line of a symbol's append-only contract-selection history."""

    symbol: str
    run_id: str
    selection_id: str
    as_of: datetime
    generated_at: datetime
    status: str
    strategy: str | None = None
    expiration: str | None = None
    dte: int | None = None
    legs: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "run_id": self.run_id,
            "selection_id": self.selection_id,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "generated_at": self.generated_at.astimezone(UTC).isoformat(),
            "status": self.status,
            "strategy": self.strategy,
            "expiration": self.expiration,
            "dte": self.dte,
            "legs": self.legs,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SymbolSelectionEntry:
        return cls(
            symbol=str(payload["symbol"]),
            run_id=str(payload["run_id"]),
            selection_id=str(payload["selection_id"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            generated_at=datetime.fromisoformat(str(payload["generated_at"])),
            status=str(payload["status"]),
            strategy=payload.get("strategy"),
            expiration=payload.get("expiration"),
            dte=payload.get("dte"),
            legs=int(payload.get("legs", 0)),
        )

    @classmethod
    def of(cls, selection: ContractSelectionResult) -> SymbolSelectionEntry:
        return cls(
            symbol=selection.symbol,
            run_id=selection.run_id,
            selection_id=selection.selection_id,
            as_of=selection.as_of,
            generated_at=selection.generated_at,
            status=selection.selection_status.value,
            strategy=selection.strategy.value if selection.strategy else None,
            expiration=selection.expiration.isoformat() if selection.expiration else None,
            dte=selection.dte,
            legs=len(selection.legs),
        )


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------
class StrategyRepository(ABC):
    """Storage interface for strategy runs."""

    @abstractmethod
    def save(self, result: StrategyRunResult) -> str:
        """Persist a run immutably. Returns its storage id."""

    @abstractmethod
    def get(self, run_id: str) -> StrategyRunResult | None: ...

    @abstractmethod
    def latest(self) -> StrategyRunResult | None: ...

    @abstractmethod
    def history(self, limit: int | None = None) -> list[StrategyHistoryEntry]: ...

    @abstractmethod
    def symbol_history(self, symbol: str, limit: int | None = None) -> list[SymbolDecisionEntry]:
        """One underlying's decision history, newest first."""


class ContractSelectionRepository(ABC):
    """Storage interface for contract-selection runs."""

    @abstractmethod
    def save(self, result: ContractSelectionRunResult) -> str: ...

    @abstractmethod
    def get(self, run_id: str) -> ContractSelectionRunResult | None: ...

    @abstractmethod
    def latest(self) -> ContractSelectionRunResult | None: ...

    @abstractmethod
    def history(self, limit: int | None = None) -> list[ContractHistoryEntry]: ...

    @abstractmethod
    def symbol_history(self, symbol: str, limit: int | None = None) -> list[SymbolSelectionEntry]:
        """One underlying's selection history, newest first."""


# ---------------------------------------------------------------------------
# Filesystem implementations
# ---------------------------------------------------------------------------
class FilesystemStrategyRepository(StrategyRepository):
    """JSON-on-disk store, rooted at ``<data>/strategy/``."""

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

    def save(self, result: StrategyRunResult) -> str:
        path = self.runs_dir / _run_filename(result.generated_at, result.run_id)
        payload = result.model_dump(mode="json")
        if path.exists():
            stored = _read_json(path)
            if stored != payload:
                raise StrategyStoreError(
                    f"refusing to overwrite strategy run {result.run_id}: a stored run is "
                    f"immutable and the existing record differs from the new one"
                )
            return str(path.relative_to(self._root))

        _write_json(path, payload)
        _append_line(self.history_path, StrategyHistoryEntry.of(result).to_json())
        for decision in result.decisions:
            _append_line(
                self.symbol_path(decision.symbol), SymbolDecisionEntry.of(decision).to_json()
            )
        return str(path.relative_to(self._root))

    def get(self, run_id: str) -> StrategyRunResult | None:
        for entry in self.history():
            if entry.run_id == run_id:
                return self._load(entry.generated_at, entry.run_id)
        return None

    def latest(self) -> StrategyRunResult | None:
        entries = self.history()
        if not entries:
            return None
        return self._load(entries[0].generated_at, entries[0].run_id)

    def history(self, limit: int | None = None) -> list[StrategyHistoryEntry]:
        entries = [
            StrategyHistoryEntry.from_json(payload)
            for payload in _read_lines(self.history_path, "strategy history")
        ]
        entries.sort(key=lambda e: (e.generated_at, e.run_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def symbol_history(self, symbol: str, limit: int | None = None) -> list[SymbolDecisionEntry]:
        entries = [
            SymbolDecisionEntry.from_json(payload)
            for payload in _read_lines(self.symbol_path(symbol), f"{symbol} decision history")
        ]
        entries.sort(key=lambda e: (e.generated_at, e.run_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def _load(self, generated_at: datetime, run_id: str) -> StrategyRunResult:
        path = self.runs_dir / _run_filename(generated_at, run_id)
        if not path.exists():
            raise StrategyStoreError(f"strategy run file is missing: {path}")
        try:
            return StrategyRunResult.model_validate(_read_json(path))
        except StrategyStoreError:
            raise
        except Exception as exc:
            raise StrategyStoreError(
                f"stored strategy run {run_id} does not match the current model "
                f"(schema drift): {exc}"
            ) from exc


class FilesystemContractSelectionRepository(ContractSelectionRepository):
    """JSON-on-disk store, rooted at ``<data>/contracts/``."""

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

    def save(self, result: ContractSelectionRunResult) -> str:
        path = self.runs_dir / _run_filename(result.generated_at, result.run_id)
        payload = result.model_dump(mode="json")
        if path.exists():
            stored = _read_json(path)
            if stored != payload:
                raise StrategyStoreError(
                    f"refusing to overwrite contract run {result.run_id}: a stored run is "
                    f"immutable and the existing record differs from the new one"
                )
            return str(path.relative_to(self._root))

        _write_json(path, payload)
        _append_line(self.history_path, ContractHistoryEntry.of(result).to_json())
        for selection in result.selections:
            _append_line(
                self.symbol_path(selection.symbol), SymbolSelectionEntry.of(selection).to_json()
            )
        return str(path.relative_to(self._root))

    def get(self, run_id: str) -> ContractSelectionRunResult | None:
        for entry in self.history():
            if entry.run_id == run_id:
                return self._load(entry.generated_at, entry.run_id)
        return None

    def latest(self) -> ContractSelectionRunResult | None:
        entries = self.history()
        if not entries:
            return None
        return self._load(entries[0].generated_at, entries[0].run_id)

    def history(self, limit: int | None = None) -> list[ContractHistoryEntry]:
        entries = [
            ContractHistoryEntry.from_json(payload)
            for payload in _read_lines(self.history_path, "contract history")
        ]
        entries.sort(key=lambda e: (e.generated_at, e.run_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def symbol_history(self, symbol: str, limit: int | None = None) -> list[SymbolSelectionEntry]:
        entries = [
            SymbolSelectionEntry.from_json(payload)
            for payload in _read_lines(self.symbol_path(symbol), f"{symbol} selection history")
        ]
        entries.sort(key=lambda e: (e.generated_at, e.run_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def _load(self, generated_at: datetime, run_id: str) -> ContractSelectionRunResult:
        path = self.runs_dir / _run_filename(generated_at, run_id)
        if not path.exists():
            raise StrategyStoreError(f"contract run file is missing: {path}")
        try:
            return ContractSelectionRunResult.model_validate(_read_json(path))
        except StrategyStoreError:
            raise
        except Exception as exc:
            raise StrategyStoreError(
                f"stored contract run {run_id} does not match the current model "
                f"(schema drift): {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------
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
            raise StrategyStoreError(f"corrupt {label} line {path}:{number}: {exc}") from exc
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
        raise StrategyStoreError(f"corrupt stored run {path}: {exc}") from exc
