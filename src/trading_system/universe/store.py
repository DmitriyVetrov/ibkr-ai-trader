"""Persistence for universe runs.

Every run is stored, and no run replaces another. That is the point: the value
of a universe history is answering "what did the system consider on 10 August,
and why did it choose what it chose" *later*, when the configuration, the data
and the model have all moved on. A store that kept only the latest universe
would make every past research decision unexplainable.

The discipline mirrors the data layer's snapshot store, for the same reasons:

* **Immutable.** A run file is written once. Writing different content to an
  existing id raises rather than overwriting — a record that changed after the
  fact is worse than no record.
* **Append-only index.** ``history.jsonl`` gains a line per run and is never
  rewritten, so a later failed run cannot erase the evidence of an earlier
  successful one.
* **Atomic.** Written via a temporary file and ``os.replace``, because a
  half-written run is indistinguishable from a truncated one and both would be
  read back as history.

:class:`UniverseRepository` is the interface consumers see. The filesystem
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

from trading_system.universe.models import UniverseSelectionResult

__all__ = [
    "FilesystemUniverseRepository",
    "UniverseHistoryEntry",
    "UniverseRepository",
    "UniverseStoreError",
]

_COMPACT_TIME = "%Y%m%dT%H%M%S%fZ"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class UniverseStoreError(RuntimeError):
    """A universe run could not be stored or read back."""


@dataclass(frozen=True, slots=True)
class UniverseHistoryEntry:
    """One line of the append-only run history.

    Enough to answer "which runs exist and how did they end" without loading
    any run file, which is what makes ``universe history`` cheap.
    """

    run_id: str
    snapshot_id: str
    as_of: datetime
    generated_at: datetime
    status: str
    selection_method: str
    selected_count: int
    rejected_count: int
    model_name: str | None = None
    prompt_version: str | None = None
    dry_run: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "generated_at": self.generated_at.astimezone(UTC).isoformat(),
            "status": self.status,
            "selection_method": self.selection_method,
            "selected_count": self.selected_count,
            "rejected_count": self.rejected_count,
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
            "dry_run": self.dry_run,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> UniverseHistoryEntry:
        return cls(
            run_id=str(payload["run_id"]),
            snapshot_id=str(payload["snapshot_id"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            generated_at=datetime.fromisoformat(str(payload["generated_at"])),
            status=str(payload["status"]),
            selection_method=str(payload["selection_method"]),
            selected_count=int(payload["selected_count"]),
            rejected_count=int(payload["rejected_count"]),
            model_name=payload.get("model_name"),
            prompt_version=payload.get("prompt_version"),
            dry_run=bool(payload.get("dry_run", False)),
        )

    @classmethod
    def of(cls, result: UniverseSelectionResult, *, dry_run: bool = False) -> UniverseHistoryEntry:
        return cls(
            run_id=result.run_id,
            snapshot_id=result.snapshot_id,
            as_of=result.as_of,
            generated_at=result.generated_at,
            status=result.status.value,
            selection_method=result.selection_method.value,
            selected_count=len(result.selected_assets),
            rejected_count=len(result.rejected_assets),
            model_name=result.agent_metadata.model_name if result.agent_metadata else None,
            prompt_version=(
                result.agent_metadata.prompt_version if result.agent_metadata else None
            ),
            dry_run=dry_run,
        )


class UniverseRepository(ABC):
    """Storage interface for universe runs.

    No path, no SQL and no serialisation detail appears in these signatures —
    that is what keeps the backend replaceable.
    """

    @abstractmethod
    def save(self, result: UniverseSelectionResult) -> str:
        """Persist a run immutably. Returns its storage id."""

    @abstractmethod
    def get(self, run_id: str) -> UniverseSelectionResult | None:
        """One run by its ``run_id``, or ``None``."""

    @abstractmethod
    def latest(self) -> UniverseSelectionResult | None:
        """The most recently generated run, or ``None``."""

    @abstractmethod
    def history(self, limit: int | None = None) -> list[UniverseHistoryEntry]:
        """Run history, newest first."""


class FilesystemUniverseRepository(UniverseRepository):
    """JSON-on-disk store, rooted at ``<data>/universe/``.

    Chosen for the same reason as the data layer's snapshot store: a universe
    run is an artifact a human will want to read with ``cat`` and diff in
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

    # --- writing -----------------------------------------------------------
    def save(self, result: UniverseSelectionResult) -> str:
        path = self._run_path(result)
        payload = result.model_dump(mode="json")

        if path.exists():
            stored = _read_json(path)
            if stored != payload:
                raise UniverseStoreError(
                    f"refusing to overwrite universe run {result.run_id}: a stored run is "
                    f"immutable and the existing record differs from the new one"
                )
            return str(path.relative_to(self._root))

        _write_json(path, payload)
        self._append_history(UniverseHistoryEntry.of(result))
        return str(path.relative_to(self._root))

    def _append_history(self, entry: UniverseHistoryEntry) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.to_json(), separators=(",", ":"), sort_keys=True)
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # --- reading -----------------------------------------------------------
    def get(self, run_id: str) -> UniverseSelectionResult | None:
        for entry in self.history():
            if entry.run_id == run_id:
                return self._load(entry)
        return None

    def latest(self) -> UniverseSelectionResult | None:
        entries = self.history()
        return self._load(entries[0]) if entries else None

    def history(self, limit: int | None = None) -> list[UniverseHistoryEntry]:
        if not self.history_path.exists():
            return []
        entries: list[UniverseHistoryEntry] = []
        for number, line in enumerate(
            self.history_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                entries.append(UniverseHistoryEntry.from_json(json.loads(line)))
            except Exception as exc:
                raise UniverseStoreError(
                    f"corrupt universe history line {self.history_path}:{number}: {exc}"
                ) from exc
        entries.sort(key=lambda e: (e.generated_at, e.run_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def _load(self, entry: UniverseHistoryEntry) -> UniverseSelectionResult:
        path = self.runs_dir / _run_filename(entry.generated_at, entry.run_id)
        if not path.exists():
            raise UniverseStoreError(f"universe run file is missing: {path}")
        try:
            return UniverseSelectionResult.model_validate(_read_json(path))
        except UniverseStoreError:
            raise
        except Exception as exc:
            raise UniverseStoreError(
                f"stored universe run {entry.run_id} does not match the current model "
                f"(schema drift): {exc}"
            ) from exc

    def _run_path(self, result: UniverseSelectionResult) -> Path:
        return self.runs_dir / _run_filename(result.generated_at, result.run_id)


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------
def _run_filename(generated_at: datetime, run_id: str) -> str:
    return f"{generated_at.astimezone(UTC).strftime(_COMPACT_TIME)}-{_SAFE.sub('_', run_id)}.json"


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
        raise UniverseStoreError(f"corrupt stored universe run {path}: {exc}") from exc
