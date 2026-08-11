"""Persistence for research runs and reports.

Every run is stored, and no run replaces another. That is the point: the value
of a research history is answering *later* — when the configuration, the data
and the model have all moved on — what the system believed on 11 August, which
evidence it believed it on, and whether the thesis was subsequently
invalidated. A store that kept only the latest view would make every past
decision unexplainable and every evaluation circular.

The discipline mirrors the universe and data stores, for the same reasons:

* **Immutable.** A run file is written once. Writing different content to an
  existing id raises rather than overwriting — a record that changed after the
  fact is worse than no record.
* **Append-only indexes.** ``history.jsonl`` gains a line per run and
  ``symbols/<SYMBOL>.jsonl`` gains a line per report. Neither is ever
  rewritten, so a later failed run cannot erase the evidence of an earlier
  successful one.
* **Atomic.** Written via a temporary file and ``os.replace``, because a
  half-written run is indistinguishable from a truncated one and both would be
  read back as history.

:class:`ResearchRepository` is the interface consumers see. The filesystem
implementation is one option; SQLite or PostgreSQL remain possible without a
consumer noticing, which is why nothing outside this module handles a path.
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

from trading_system.research.models import MarketResearchReport, ResearchRunResult

__all__ = [
    "FilesystemResearchRepository",
    "ResearchHistoryEntry",
    "ResearchRepository",
    "ResearchStoreError",
    "SymbolHistoryEntry",
]

_COMPACT_TIME = "%Y%m%dT%H%M%S%fZ"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class ResearchStoreError(RuntimeError):
    """A research run could not be stored or read back."""


@dataclass(frozen=True, slots=True)
class ResearchHistoryEntry:
    """One line of the append-only run history.

    Enough to answer "which runs exist and how did they end" without loading a
    run file, which is what makes ``research history`` cheap.
    """

    run_id: str
    as_of: datetime
    generated_at: datetime
    status: str
    symbols: tuple[str, ...]
    succeeded: int
    failed: int
    model_name: str | None = None
    prompt_version: str | None = None
    universe_run_id: str | None = None
    dry_run: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "generated_at": self.generated_at.astimezone(UTC).isoformat(),
            "status": self.status,
            "symbols": list(self.symbols),
            "succeeded": self.succeeded,
            "failed": self.failed,
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
            "universe_run_id": self.universe_run_id,
            "dry_run": self.dry_run,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ResearchHistoryEntry:
        return cls(
            run_id=str(payload["run_id"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            generated_at=datetime.fromisoformat(str(payload["generated_at"])),
            status=str(payload["status"]),
            symbols=tuple(str(s) for s in payload.get("symbols", [])),
            succeeded=int(payload.get("succeeded", 0)),
            failed=int(payload.get("failed", 0)),
            model_name=payload.get("model_name"),
            prompt_version=payload.get("prompt_version"),
            universe_run_id=payload.get("universe_run_id"),
            dry_run=bool(payload.get("dry_run", False)),
        )

    @classmethod
    def of(cls, result: ResearchRunResult, *, dry_run: bool = False) -> ResearchHistoryEntry:
        model = next(
            (r.agent_metadata for r in result.reports if r.agent_metadata is not None), None
        )
        return cls(
            run_id=result.run_id,
            as_of=result.as_of,
            generated_at=result.generated_at,
            status=result.status.value,
            symbols=tuple(result.symbols),
            succeeded=result.counts.succeeded,
            failed=result.counts.failed,
            model_name=model.model_name if model else None,
            prompt_version=model.prompt_version if model else None,
            universe_run_id=result.universe_run_id,
            dry_run=dry_run,
        )


@dataclass(frozen=True, slots=True)
class SymbolHistoryEntry:
    """One line of a symbol's append-only report history.

    Lets "what did we think about NVDA over time, and did the view change" be
    answered without reading every run in the store.
    """

    symbol: str
    run_id: str
    report_id: str
    as_of: datetime
    generated_at: datetime
    status: str
    hypothesis: str | None = None
    confidence: str | None = None
    horizon_days: int | None = None
    evidence_count: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "run_id": self.run_id,
            "report_id": self.report_id,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "generated_at": self.generated_at.astimezone(UTC).isoformat(),
            "status": self.status,
            "hypothesis": self.hypothesis,
            "confidence": self.confidence,
            "horizon_days": self.horizon_days,
            "evidence_count": self.evidence_count,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SymbolHistoryEntry:
        return cls(
            symbol=str(payload["symbol"]),
            run_id=str(payload["run_id"]),
            report_id=str(payload["report_id"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            generated_at=datetime.fromisoformat(str(payload["generated_at"])),
            status=str(payload["status"]),
            hypothesis=payload.get("hypothesis"),
            confidence=payload.get("confidence"),
            horizon_days=payload.get("horizon_days"),
            evidence_count=int(payload.get("evidence_count", 0)),
        )

    @classmethod
    def of(cls, report: MarketResearchReport) -> SymbolHistoryEntry:
        return cls(
            symbol=report.symbol,
            run_id=report.run_id,
            report_id=report.report_id,
            as_of=report.as_of,
            generated_at=report.generated_at,
            status=report.status.value,
            hypothesis=report.hypothesis.value if report.hypothesis else None,
            confidence=report.confidence.value if report.confidence else None,
            horizon_days=report.horizon_days,
            evidence_count=len(report.evidence),
        )


class ResearchRepository(ABC):
    """Storage interface for research runs.

    No path, no SQL and no serialisation detail appears in these signatures —
    that is what keeps the backend replaceable.
    """

    @abstractmethod
    def save(self, result: ResearchRunResult) -> str:
        """Persist a run immutably. Returns its storage id."""

    @abstractmethod
    def get(self, run_id: str) -> ResearchRunResult | None:
        """One run by its ``run_id``, or ``None``."""

    @abstractmethod
    def latest(self) -> ResearchRunResult | None:
        """The most recently generated run, or ``None``."""

    @abstractmethod
    def history(self, limit: int | None = None) -> list[ResearchHistoryEntry]:
        """Run history, newest first."""

    @abstractmethod
    def symbol_history(self, symbol: str, limit: int | None = None) -> list[SymbolHistoryEntry]:
        """One underlying's report history, newest first."""


class FilesystemResearchRepository(ResearchRepository):
    """JSON-on-disk store, rooted at ``<data>/research/``.

    Chosen for the same reason as the universe and data stores: a research
    report is an artifact a human will want to read with ``cat`` and diff in
    review, and an immutable record maps naturally onto a write-once file.
    """

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

    # --- writing -----------------------------------------------------------
    def save(self, result: ResearchRunResult) -> str:
        path = self._run_path(result)
        payload = result.model_dump(mode="json")

        if path.exists():
            stored = _read_json(path)
            if stored != payload:
                raise ResearchStoreError(
                    f"refusing to overwrite research run {result.run_id}: a stored run is "
                    f"immutable and the existing record differs from the new one"
                )
            return str(path.relative_to(self._root))

        _write_json(path, payload)
        _append_line(self.history_path, ResearchHistoryEntry.of(result).to_json())
        for report in result.reports:
            _append_line(self.symbol_path(report.symbol), SymbolHistoryEntry.of(report).to_json())
        return str(path.relative_to(self._root))

    # --- reading -----------------------------------------------------------
    def get(self, run_id: str) -> ResearchRunResult | None:
        for entry in self.history():
            if entry.run_id == run_id:
                return self._load(entry)
        return None

    def latest(self) -> ResearchRunResult | None:
        entries = self.history()
        return self._load(entries[0]) if entries else None

    def history(self, limit: int | None = None) -> list[ResearchHistoryEntry]:
        entries = [
            ResearchHistoryEntry.from_json(payload)
            for payload in _read_lines(self.history_path, "research history")
        ]
        entries.sort(key=lambda e: (e.generated_at, e.run_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def symbol_history(self, symbol: str, limit: int | None = None) -> list[SymbolHistoryEntry]:
        entries = [
            SymbolHistoryEntry.from_json(payload)
            for payload in _read_lines(self.symbol_path(symbol), f"{symbol} research history")
        ]
        entries.sort(key=lambda e: (e.generated_at, e.run_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def _load(self, entry: ResearchHistoryEntry) -> ResearchRunResult:
        path = self.runs_dir / _run_filename(entry.generated_at, entry.run_id)
        if not path.exists():
            raise ResearchStoreError(f"research run file is missing: {path}")
        try:
            return ResearchRunResult.model_validate(_read_json(path))
        except ResearchStoreError:
            raise
        except Exception as exc:
            raise ResearchStoreError(
                f"stored research run {entry.run_id} does not match the current model "
                f"(schema drift): {exc}"
            ) from exc

    def _run_path(self, result: ResearchRunResult) -> Path:
        return self.runs_dir / _run_filename(result.generated_at, result.run_id)


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
            raise ResearchStoreError(f"corrupt {label} line {path}:{number}: {exc}") from exc
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
        raise ResearchStoreError(f"corrupt stored research run {path}: {exc}") from exc
